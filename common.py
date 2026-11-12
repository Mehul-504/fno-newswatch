"""Shared helpers: time, state files, secrets, NSE website client, LLM gateway, Telegram bot."""

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("NW_STATE", os.path.join(HERE, "state"))
# On the Mac the credentials live in "Ai Trading/fno"; in the cloud they come from environment variables (secrets).
LOCAL_FNO = os.environ.get("NW_LOCAL_FNO", os.path.join(os.path.dirname(HERE), "fno"))


def now():
    return datetime.now(IST)


def log(msg):
    print(now().strftime("%H:%M:%S"), msg, flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def secret(name, default=None):
    """Environment first (cloud), then the local credential files (Mac). Values are never printed."""
    v = os.environ.get(name)
    if v:
        return v
    s = load_json(os.path.join(LOCAL_FNO, "secrets.json"), {})
    llm = load_json(os.path.join(LOCAL_FNO, "llm_creds.json"), {})
    br = load_json(os.path.join(LOCAL_FNO, "breeze_creds.json"), {})
    local = {"TELEGRAM_API_ID": s.get("TELEGRAM_API_ID"), "TELEGRAM_API_HASH": s.get("TELEGRAM_API_HASH"),
             "TELEGRAM_BOT_TOKEN": s.get("TELEGRAM_BOT_TOKEN"), "TELEGRAM_CHAT_ID": s.get("TELEGRAM_CHAT_ID"),
             "LLM_URL": llm.get("chat_url") or llm.get("base_url"), "LLM_KEY": llm.get("api_key"),
             "LLM_MODEL": llm.get("model"), "LLM_BACKUP_MODEL": llm.get("backup_model"),
             "BREEZE_API_KEY": br.get("api_key"), "BREEZE_API_SECRET": br.get("api_secret")}
    return local.get(name) or default


# ------------------------------------------------------------------ NSE website (free, no login)

class Nse:
    BASE = "https://www.nseindia.com"
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/129.0 Safari/537.36")

    def __init__(self, gap=1.2):
        self.gap, self.last, self.warm = gap, 0.0, 0.0
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _get(self, url, referer=None):
        wait = self.gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        h = {"User-Agent": self.UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
             "Accept-Language": "en-US,en;q=0.9"}
        if referer:
            h["Referer"] = referer
        try:
            with self.opener.open(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read()
        finally:
            self.last = time.monotonic()

    def api(self, path):
        """NSE's API answers only with the cookies its pages set; refresh them every 10 minutes."""
        for attempt in (0, 1):
            if attempt or time.monotonic() - self.warm > 600:
                self._get(self.BASE + "/option-chain")
                self.warm = time.monotonic()
            try:
                return json.loads(self._get(self.BASE + path, referer=self.BASE + "/option-chain").decode())
            except (urllib.error.URLError, ValueError, OSError) as e:
                if attempt:
                    raise RuntimeError(f"NSE {path}: {str(e)[:80]}")

    def announcements(self):
        d = now().strftime("%d-%m-%Y")
        return self.api(f"/api/corporate-announcements?index=equities&from_date={d}&to_date={d}") or []

    def chart(self, sym):
        """Today's minute prices: list of (datetime IST, price)."""
        d = self.api("/api/chart-databyindex-dynamic?index=%sEQN&type=symbol" % urllib.parse.quote(sym))
        out = []
        for p in d.get("grapthData") or []:
            if len(p) >= 2 and (len(p) < 3 or p[2] != "PO"):
                out.append((datetime.fromtimestamp(p[0] / 1000, timezone.utc).replace(tzinfo=None) + timedelta(hours=0), float(p[1])))
        # NSE stamps these points in IST already (epoch-ms of the IST wall clock)
        return [(t, px) for t, px in out], float(d.get("closePrice") or 0)

    def expiries(self, sym):
        d = self.api("/api/option-chain-contract-info?symbol=%s" % urllib.parse.quote(sym))
        return sorted(datetime.strptime(e, "%d-%b-%Y").date() for e in d.get("expiryDates") or [])

    def chain(self, sym, expiry):
        d = self.api("/api/option-chain-v3?type=Equity&symbol=%s&expiry=%s" % (
            urllib.parse.quote(sym), expiry.strftime("%d-%b-%Y")))
        rec = d.get("records") or {}
        rows = rec.get("data") or (d.get("filtered") or {}).get("data") or []
        spot = float(rec.get("underlyingValue") or 0)
        out = []
        for r in rows:
            row = {"strike": float(r.get("strikePrice") or 0)}
            for side in ("CE", "PE"):
                leg = r.get(side) or {}
                spot = spot or float(leg.get("underlyingValue") or 0)
                row[side] = {"ltp": float(leg.get("lastPrice") or 0), "bid": float(leg.get("buyPrice1") or 0),
                             "ask": float(leg.get("sellPrice1") or 0), "iv": float(leg.get("impliedVolatility") or 0),
                             "vol": float(leg.get("totalTradedVolume") or 0), "oi": float(leg.get("openInterest") or 0)}
            out.append(row)
        return spot, sorted(out, key=lambda x: x["strike"])


# ------------------------------------------------------------------ LLM gateway (OpenAI-compatible)

LLM_STATS = {}   # model -> {"ok": n, "fail": n}, shown by /status


def llm_chat(messages, max_tokens=700, timeout=90, models=None):
    url, key = secret("LLM_URL"), secret("LLM_KEY")
    if not (url and key):
        raise RuntimeError("no LLM credentials")
    url = url.rstrip("/")
    if not url.endswith("/completions"):
        url = url + ("/completions" if url.endswith("/chat") else "/chat/completions")
    import requests  # the gateway's Cloudflare front rejects urllib's default client (error 1010)
    models = models or [m for m in (secret("LLM_MODEL", "deepseek-expert"), secret("LLM_BACKUP_MODEL")) if m]
    last = None
    for m in models:
        try:
            r = requests.post(url, timeout=timeout, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                              json={"model": m, "temperature": 0, "max_tokens": max_tokens, "messages": messages})
            if r.status_code == 200:
                j = r.json()
                LLM_STATS.setdefault(m, {"ok": 0, "fail": 0})["ok"] += 1
                return j["choices"][0]["message"]["content"] if "choices" in j else j.get("content", "")
            LLM_STATS.setdefault(m, {"ok": 0, "fail": 0})["fail"] += 1
            last = f"{m} HTTP {r.status_code}"
            if r.status_code == 429:
                break
        except (requests.RequestException, ValueError, KeyError) as e:
            LLM_STATS.setdefault(m, {"ok": 0, "fail": 0})["fail"] += 1
            last = f"{m} {type(e).__name__}"
    raise RuntimeError(f"LLM failed: {last}")


def first_json(text):
    m = re.search(r"(\[.*\]|\{.*\})", text or "", re.S)
    if not m:
        raise ValueError("no JSON in reply")
    return json.loads(m.group(1), strict=False)


# ------------------------------------------------------------------ Telegram bot (alerts out, commands in)

class Bot:
    API = "https://api.telegram.org/bot%s/%s"

    def __init__(self, dry_run=False):
        self.token, self.chat = secret("TELEGRAM_BOT_TOKEN"), str(secret("TELEGRAM_CHAT_ID") or "")
        self.dry = dry_run or not (self.token and self.chat)

    def _call(self, method, payload):
        req = urllib.request.Request(self.API % (self.token, method), data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode())

    def send(self, text):
        print("\n" + "-" * 60 + "\n" + text + "\n" + "-" * 60, flush=True)
        if self.dry:
            return True
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
            for attempt in range(4):   # connection resets happen; retry before giving up
                try:
                    self._call("sendMessage", {"chat_id": self.chat, "text": chunk, "parse_mode": "HTML",
                                               "disable_web_page_preview": True})
                    break
                except (urllib.error.URLError, OSError, ValueError) as e:
                    print(f"telegram send failed (try {attempt + 1}):", str(e)[:120], file=sys.stderr)
                    time.sleep(3 * (attempt + 1))
            else:
                return False
        return True

    def commands(self, offset):
        """New messages from the owner's chat only: returns (next offset, [text, ...])."""
        if self.dry:
            return offset, []
        try:
            j = self._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})
        except (urllib.error.URLError, OSError, ValueError):
            return offset, []
        out = []
        for u in j.get("result", []):
            offset = max(offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) == self.chat and msg.get("text"):
                out.append(msg["text"].strip())
        return offset, out


def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
