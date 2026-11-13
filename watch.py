#!/usr/bin/env python3
"""F&O news watch: Telegram channels + NSE filings -> AI read -> Telegram alerts with a trade trigger.

    python watch.py run --until 15:40          poll until 15:40 IST (prices from Breeze if a session works, else NSE)
    python watch.py run --until 15:40 --dry    print alerts instead of sending them
    python watch.py once --backfill 120        one round over the last 120 minutes (test)
    python watch.py levels                     refresh previous-day close / ATR (state/levels.json)

Bot commands (from your own chat only): /session <Breeze session id>  /status  /digest  /help
Never places orders. Telegram channels are read only (see sources.py).
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LLM_STATS, STATE, Bot, esc, load_json, log, now, save_json, secret  # noqa: E402
import fno  # noqa: E402
import grading  # noqa: E402
import market as M  # noqa: E402
import sources  # noqa: E402
import suggest  # noqa: E402
import tracker  # noqa: E402
import breakouts as BO  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = load_json(os.path.join(HERE, "config.json"), {})
STATE_FILE = os.path.join(STATE, "state.json")


# ------------------------------------------------------------------ small helpers

def _key():
    k = secret("STATE_KEY")
    return hashlib.sha256(k.encode()).digest() if k else None


def seal(text):
    """Keep the Breeze session out of the committed state in the cloud (XOR with a key from the secrets)."""
    k = _key()
    if not k:
        return {"plain": text}
    b = text.encode()
    return {"sealed": base64.b64encode(bytes(c ^ k[i % len(k)] for i, c in enumerate(b))).decode()}


def unseal(obj):
    if not obj:
        return None
    if "plain" in obj:
        return obj["plain"]
    k = _key()
    if not k or "sealed" not in obj:
        return None
    b = base64.b64decode(obj["sealed"])
    return bytes(c ^ k[i % len(k)] for i, c in enumerate(b)).decode()


def breeze_token(state):
    s = state.get("breeze")
    if s and s.get("day") == str(now().date()):
        return unseal(s.get("token"))
    env = secret("BREEZE_SESSION")
    if env:
        return env
    local = load_json(os.path.join(os.path.dirname(HERE), "fno", "breeze_session.json"), {})
    if local.get("created", "")[:10] == str(now().date()):
        return local.get("session_token")
    return None


def text_key(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())[:70]


MEANINGFUL = {"results", "order", "broker", "regulatory", "management", "block deal", "bulk deal", "stake sale",
              "corporate", "tip/call"}
PRICE_TALK = re.compile(r"\b(up|down|rises?|falls?|jumps?|surges?|gains?|slips?|rall(y|ies)|tumbles?)\b[^.]{0,25}\d+(\.\d+)?\s*(%|percent)", re.I)


def price_mismatch(text, spot):
    """A post quoting a share price far from this stock's price is about another company (e.g. another 'Apollo')."""
    if not spot:
        return False
    nums = [float(x.replace(",", "")) for x in re.findall(r"(?:₹|rs\.?|inr|\bto\b|\bat\b)\s*([0-9][0-9,]*\.?[0-9]*)", text, re.I)]
    nums = [n for n in nums if 5 <= n <= 200000]
    return bool(nums) and all(abs(n / spot - 1) > 0.15 for n in nums)


def model_ranks():
    return load_json(os.path.join(STATE, "model_rank.json"), {}).get("ranks", {})


# ------------------------------------------------------------------ the watcher

class Watch:
    def __init__(self, dry=False):
        self.state = load_json(STATE_FILE, {})
        self.uni = fno.load()
        self.bot = Bot(dry_run=dry)
        self.market = M.Market(self.uni, load_json(os.path.join(STATE, "levels.json"), {}))
        chans = list(CFG.get("news_channels", []))
        self.tips = {c.lower() for c in CFG.get("tip_channels", [])}
        if CFG.get("include_tip_channels"):
            chans += CFG.get("tip_channels", [])
        self.tg = sources.Telegram(chans, self.state)
        self.filings = sources.Filings(self.uni, self.state)
        self.budget = float(CFG.get("budget_rs", 20000))
        self.day = str(now().date())
        if self.state.get("day") != self.day:
            self.state.update(day=self.day, sent={}, recent={}, digest_done=[], today=[])
        self.ranks = model_ranks()
        self.tracker = tracker.Tracker(self.state, self.market, self.uni, self.budget, CFG.get("learning", "auto"))
        self.tracker.roll()

    def save(self):
        save_json(STATE_FILE, self.state)

    def log_item(self, row):
        path = os.path.join(STATE, "alerts", f"{self.day}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")

    def handle(self, post):
        items = [(s, post["text"]) for s in post["syms"]] if post.get("syms") else fno.match(post["text"], self.uni)
        if not items:
            return 0
        recent = self.state.setdefault("recent", {})
        keep = []
        for s, ctx in items:
            k = f"{s}|{text_key(ctx)}"
            if k in recent:
                continue
            recent[k] = str(post["ts"])
            keep.append((s, ctx))
        if not keep:
            return 0
        grades = grading.grade(keep, self.uni)
        sent = 0
        for s, ctx in keep:
            g = grades.get(s)
            if not g:
                continue
            row = dict(t=now(), post_time=post["ts"], source=post["source"], text=ctx[:500], **g)
            if post["source"].lower() in self.tips:
                g["event_type"] = "tip/call"
            if g["direction"] == 0:
                self.log_item(dict(row, alerted=False, why="no direction"))
                continue
            snap = self.market.snapshot(s)
            ref = snap.get("spot") or snap.get("pc")
            tip = g.get("tip") or {}
            tip_off = ref and tip.get("right") in (None, "STOCK", "FUT") and any(
                tip.get(k) and abs(tip[k] / ref - 1) > 0.15 for k in ("entry", "target", "stop"))
            if price_mismatch(ctx, ref) or tip_off:
                self.log_item(dict(row, alerted=False, why="price in post does not match this stock"))
                continue
            side = "CE" if g["direction"] > 0 else "PE"
            why = None
            if g["strength"] == "weak" and (g["event_type"] not in MEANINGFUL or PRICE_TALK.search(ctx)):
                why = "price talk / weak non-event"
            elif g["strength"] == "weak" and not CFG.get("alert_weak_news", True):
                why = "weak news switched off"
            tag = f"{s}|{g['direction']}"
            last = self.state["sent"].get(tag)
            if not why and last and now() - datetime.fromisoformat(last["t"]) < timedelta(hours=CFG.get("duplicate_hours", 1)) \
                    and not (g["strength"] == "strong" and last.get("strength") != "strong"):
                why = "duplicate within the hour"
            if not why:
                like = dict(source=post["source"], event_type=g["event_type"], strength=g["strength"], side=side,
                            alert_hm=now().strftime("%H:%M"), kind="tip" if g["event_type"] == "tip/call" and g.get("tip") else "rule",
                            ran_atr=abs(snap["spot"] - snap["pc"]) / snap["atr"] if snap.get("spot") and snap.get("pc") and snap.get("atr") else None)
                m = self.tracker.muted_for(like)
                if m:
                    why = "muted by learning: " + ", ".join(m)
            opt = {}
            if why:
                self.log_item(dict(row, alerted=False, why=why))
            else:
                opt = self.market.pick_option(s, side, self.budget)
                msg = suggest.alert(s, g, post, 0, snap, opt, self.budget, self.ranks.get(s), self.market.mode())
                self.bot.send(msg)
                self.state["sent"][tag] = {"t": now().isoformat(), "strength": g["strength"]}
                self.state.setdefault("today", []).append(dict(sym=s, dir=g["direction"], strength=g["strength"],
                                                               event=g["event_type"], head=g.get("headline"), t=f"{now():%H:%M}"))
                self.log_item(dict(row, alerted=True, snap=snap, option={k: v for k, v in opt.items() if k not in ("atm", "otm")}))
                sent += 1
            if why != "duplicate within the hour":
                self.tracker.add(s, g, post, snap, opt, alerted=not why, model_rank=self.ranks.get(s))
        return sent

    def command(self, text):
        parts = text.split()
        cmd = parts[0].lower().split("@")[0] if parts else ""
        if cmd == "/session" and len(parts) >= 2:
            ok = self.market.connect_breeze(parts[1])
            if ok:
                self.state["breeze"] = {"day": self.day, "token": seal(parts[1])}
                self.save()
            self.bot.send("✅ Breeze connected — alerts now carry live Breeze prices." if ok
                          else f"❌ {esc(self.market.breeze_note)}. Alerts continue with NSE prices.")
        elif cmd == "/status":
            n = len(self.state.get("today", []))
            ai = ", ".join(f"{m} {v['ok']} ok / {v['fail']} failed" for m, v in LLM_STATS.items()) or "no AI calls yet this run"
            self.bot.send(f"News watch running · {len(self.tg.ents)} channels + NSE filings\n"
                          f"Prices: {self.market.mode()}\nAI reads: {ai}\nAlerts today: {n}\nBudget per trade: ₹{self.budget:,.0f}")
        elif cmd == "/digest":
            self.digest("on request")
        elif cmd == "/scorecard":
            self.bot.send(self.tracker.scorecard())
        elif cmd == "/watchlist":
            w = self.state.get("bo_watch") or {}
            items = sorted((w.get("items") or {}).items(), key=lambda kv: kv[1]["gap"])
            self.bot.send(f"🎯 <b>Breakout watchlist for {w.get('day', '-')}</b>\n" + ("\n".join(
                f"{esc(s)} {'↑' if it['side'] == 'up' else '↓'} ₹{it['level']:,.1f} ({it['gap'] * 100:.1f}% away, {int(it['touches'])} touches)"
                for s, it in items) or "empty — the list is built each evening"))
        elif cmd == "/learn":
            self.bot.send(self.tracker.learn(apply=False))
        elif cmd in ("/mute", "/unmute") and len(parts) >= 2 and parts[1].isdigit():
            self.bot.send(self.tracker.apply_choice(cmd[1:], int(parts[1])))
            self.save()
        elif cmd == "/open":
            live = [r for r in self.tracker.recs if r["status"] in ("waiting", "open")]
            self.bot.send("📒 <b>Paper trades in play</b>\n" + ("\n".join(
                f"{'🟡' if r['status'] == 'waiting' else '🟢'} {esc(r['sym'])} {r['side']} {r['kind']} · {r['status']}"
                + (f" · in at {r.get('entry_opt')} ({r.get('strike')})" if r["status"] == "open" else "") for r in live)
                or "none"))
        elif cmd in ("/help", "/start"):
            self.bot.send("Commands:\n/session &lt;Breeze session id&gt; — live Breeze prices for today\n"
                          "/status — what is running\n/digest — today's news so far\n/scorecard — today's paper results\n"
                          "/open — paper trades in play\n/learn — lessons from all tracked trades\n"
                          "/mute N, /unmute N — approve suggestion N from /learn\n"
                          "/watchlist — today's chart-breakout watchlist\n/help — this list")

    # ------------------------------------------------------------------ chart breakouts
    def _store(self):
        if getattr(self, "bo_store_day", None) != self.day:
            self.bo_store, self.bo_store_day = BO.load_store(), self.day
        return self.bo_store

    def evening_scan(self):
        """After NSE publishes the day's prices: breakouts on closing prices, watch-only list, tomorrow's watchlist."""
        BO.update_store(300)
        store = BO.load_store()
        latest = max(x["days"][-1] for x in store.values()) if store else None
        if str(latest) != self.day:
            log("evening scan: today's NSE file not published yet")
            return False
        self.bo_store, self.bo_store_day = store, self.day
        ev, watch = BO.scan(store, set(self.uni))
        fno_ev = sorted([e for e in ev if e["fno"]], key=lambda e: (-e["score"], -e["touches"]))
        cash = sorted([e for e in ev if not e["fno"] and e["turnover_cr"] >= 5 and e["close"] >= 20],
                      key=lambda e: (-e["score"], -e["turnover_cr"]))
        name = {"trendline": "trendline", "flat": "flat top"}
        lines = [f"📈 <b>Chart breakouts — {latest:%d %b}</b> (closing prices, daily chart)"]
        if fno_ev:
            lines.append("<b>F&amp;O</b> — paper-tracked from 9:30 tomorrow if still past the line:")
        for e in fno_ev[:12]:
            icon = "🟢" if e["side"] == "up" else "🔴"
            kind = name[e["kind"]] if e["side"] == "up" else ("rising trendline" if e["kind"] == "trendline" else "flat bottom") + " broken"
            lines.append(f"{icon} <b>{esc(e['sym'])}</b> {kind} ₹{e['level']:,.1f} → close ₹{e['close']:,.1f} · "
                         f"{e['touches']} touches, {e['span']} days · score {e['score']}/7 · {esc(e['checks']['volume'])} · "
                         f"{esc(e['checks']['room'])}")
            self.tracker.add_breakout(e["sym"], e["side"], e["level"], e["close"], True, "evening scan", e["kind"],
                                      e["score"], start_next_day=True)
        if not fno_ev:
            lines.append("No F&amp;O breakouts today.")
        if cash:
            lines.append("\n<b>Watch-only (not F&amp;O, turnover ₹5 cr+)</b>:")
            lines += [f"{'🟢' if e['side'] == 'up' else '🔴'} {esc(e['sym'])} {name[e['kind']]} ₹{e['level']:,.1f} → "
                      f"₹{e['close']:,.1f} · score {e['score']}/7 · ₹{e['turnover_cr']:,.0f} cr" for e in cash[:15]]
        nxt = sorted(watch.items(), key=lambda kv: kv[1]["gap"])[:30]
        if nxt:
            lines.append("\n🎯 <b>Tomorrow's F&amp;O watchlist</b> — alert the moment the line breaks:")
            lines.append(" · ".join(f"{esc(s)} {'↑' if w['side'] == 'up' else '↓'}₹{w['level']:,.1f} ({w['gap'] * 100:.1f}%)" for s, w in nxt))
        self.bot.send("\n".join(lines))
        self.state["bo_watch"] = {"day": str(tracker.next_weekday(now().date())),
                                  "items": {s: {k: (float(v) if isinstance(v, (int, float)) or hasattr(v, "item") else v)
                                                for k, v in w.items()} for s, w in nxt}}
        self.state["bo_alerted"] = []
        return True

    def live_breakouts(self):
        w = self.state.get("bo_watch") or {}
        if w.get("day") != self.day or not (dtime(9, 30) <= now().time() <= dtime(15, 0)):
            return
        done = self.state.setdefault("bo_alerted", [])
        for sym, it in (w.get("items") or {}).items():
            if sym in done:
                continue
            bars = self.market.series(sym)
            if not bars:
                continue
            px, up = bars[-1][3], it["side"] == "up"
            if not ((px > it["level"] * 1.005) if up else (px < it["level"] * 0.995)):
                continue
            done.append(sym)
            s = self._store().get(sym)
            checks, score = BO.live_checks(s, it["side"], px, it["level"], it) if s else ([], 0)
            opt = self.market.pick_option(sym, "CE" if up else "PE", self.budget)
            kind = ("falling trendline" if up else "rising trendline") if it["kind"] == "trendline" else ("flat top" if up else "flat bottom")
            stop = it["level"] * (0.99 if up else 1.01)
            lines = [f"{'📈' if up else '📉'} <b>{'BREAKOUT' if up else 'BREAKDOWN'} · {esc(sym)}</b> · {kind} "
                     f"({int(it['touches'])} touches, {int(it['span'])} days) · {now():%H:%M}",
                     f"Broke ₹{it['level']:,.1f} → now ₹{px:,.1f} ({abs(px / it['level'] - 1) * 100:.1f}% past the line)",
                     "📐 " + " · ".join(f"{'✅' if ok else '⚠️'} {esc(t)}" for ok, t in checks) + f" → {score}/{len(checks)}"
                     + ("" if self.market.breeze else " · volume needs Breeze"),
                     f"<b>Plan {'CE' if up else 'PE'}:</b> buy now or on a dip back to ₹{it['level']:,.1f}; stop if it closes back "
                     f"{'below' if up else 'above'} ₹{stop:,.1f}; hold up to 3 days."]
            lines += suggest.plan_text(sym, "CE" if up else "PE", {}, opt, self.budget)[-1:]
            self.bot.send("\n".join(lines))
            self.tracker.add_breakout(sym, it["side"], it["level"], px, True, "live breakout", it["kind"], score + 2)

    def digest(self, label):
        rows = self.state.get("today", [])
        if not rows:
            self.bot.send(f"📋 News digest ({label}): no graded F&O news yet today.")
            return
        by = {}
        for r in rows:
            by.setdefault(r["sym"], []).append(r)
        lines = [f"📋 <b>News digest</b> ({label}) — {len(rows)} alerts, {len(by)} stocks"]
        for s, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            net = sum(r["dir"] for r in rs)
            icon = "🟢" if net > 0 else "🔴" if net < 0 else "⚪"
            lines.append(f"{icon} <b>{esc(s)}</b>: " + "; ".join(f"{r['t']} {esc(r['head'] or r['event'])}" for r in rs[-3:]))
        self.bot.send("\n".join(lines))

    def run(self, until, backfill):
        self.market.connect_breeze(breeze_token(self.state))
        names = self.tg.start()
        offset = int(self.state.get("bot_offset", 0))
        prev_end = self.state.get("last_run_end")
        handover = prev_end and prev_end[:10] == self.day and now() - datetime.fromisoformat(prev_end) < timedelta(minutes=30)
        start_msg = (f"📡 News watch on — {len(names)} Telegram channels + NSE filings · {self.market.mode()}"
                     + ("" if self.market.breeze else "\nSend /session &lt;id&gt; after your Breeze login for live Breeze prices."))
        if handover:
            log("hand-over from the previous run: " + start_msg)   # no repeat message on every hand-over
        else:
            self.bot.send(start_msg)
        first, last_save, last_tg, last_nse, last_trk = True, 0, 0, 0, 0
        stop_at = datetime.combine(now().date(), until, tzinfo=now().tzinfo)
        while now() < stop_at:
            t = time.monotonic()
            if t - last_tg >= CFG.get("poll_seconds", 45):
                posts = self.tg.poll(backfill if first else 0)
                first, last_tg = False, t
                for p in posts:
                    self.handle(p)
            if t - last_nse >= 60:
                since = now() - timedelta(minutes=backfill) if not last_nse else None
                for p in self.filings.poll(since):
                    self.handle(p)
                last_nse = t
            if t - last_trk >= 120:
                self.tracker.update()
                try:
                    self.live_breakouts()
                except Exception as e:
                    log(f"live breakouts: {str(e)[:80]}")
                last_trk = t
            hm0 = now().strftime("%H:%M")
            done = self.state.setdefault("digest_done", [])
            if "15:16" <= hm0 < "15:40" and "close" not in done:
                self.tracker.close_day()
                done.append("close")
            if "15:45" <= hm0 < "16:30" and "score" not in done:
                self.bot.send(self.tracker.scorecard())
                done.append("score")
            if now().weekday() < 5 and hm0 >= "19:15" and "bo_scan" not in done and t - getattr(self, "bo_try", 0) > 900:
                self.bo_try = t
                try:
                    if self.evening_scan():
                        done.append("bo_scan")
                except Exception as e:
                    log(f"evening scan failed: {str(e)[:100]}")
            if now().weekday() == 4 and "16:00" <= hm0 < "17:00" and "learn" not in done:
                self.bot.send(self.tracker.learn(apply=True))
                done.append("learn")
            offset, cmds = self.bot.commands(offset)
            for c in cmds:
                self.command(c)
            self.state["bot_offset"] = offset
            hm = now().strftime("%H:%M")
            for d in CFG.get("digests", []):
                if hm >= d and d not in self.state.setdefault("digest_done", []) and hm < (datetime.strptime(d, "%H:%M") + timedelta(minutes=20)).strftime("%H:%M"):
                    self.state["digest_done"].append(d)
                    self.digest(d)
            if t - last_save > 120:
                self.state["last_run_end"] = now().isoformat()
                self.save()
                last_save = t
            time.sleep(5)
        self.state["last_run_end"] = now().isoformat()
        self.save()
        log("stopped (time up) · AI reads: " + json.dumps(LLM_STATS))


def refresh_levels():
    """Previous-day close, high, low and 14-day ATR for every F&O stock from NSE's public daily files."""
    import csv
    import io
    import urllib.request
    uni = fno.load()
    hist = {}
    day, got = now().date(), 0
    for back in range(1, 30):
        d = day - timedelta(days=back)
        if d.weekday() > 4:
            continue
        url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
            raw = urllib.request.urlopen(req, timeout=30).read().decode()
        except Exception:
            continue
        for r in csv.DictReader(io.StringIO(raw)):
            r = {k.strip(): (v or "").strip() for k, v in r.items()}
            if r.get("SERIES") == "EQ" and r.get("SYMBOL") in uni:
                try:
                    hist.setdefault(r["SYMBOL"], []).append((d, float(r["HIGH_PRICE"]), float(r["LOW_PRICE"]),
                                                             float(r["CLOSE_PRICE"]), float(r["PREV_CLOSE"])))
                except ValueError:
                    pass
        got += 1
        if got >= 16:
            break
    levels = {}
    for s, rows in hist.items():
        rows.sort()
        trs = [max(h - l, abs(h - pc), abs(l - pc)) for _, h, l, c, pc in rows[-14:]]
        _, h, l, c, _ = rows[-1]
        levels[s] = dict(pc=c, ph=h, pl=l, atr=sum(trs) / len(trs) if trs else None, asof=str(rows[-1][0]))
    save_json(os.path.join(STATE, "levels.json"), levels)
    log(f"levels: {len(levels)} stocks from {got} trading days")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run", "once", "levels"])
    ap.add_argument("--until", default="15:40")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--backfill", type=int, default=CFG.get("backfill_minutes_on_start", 30))
    a = ap.parse_args()
    if a.mode == "levels":
        refresh_levels()
        return
    w = Watch(dry=a.dry)
    if a.mode == "once":
        w.market.connect_breeze(breeze_token(w.state))
        w.tg.start()
        n = sum(w.handle(p) for p in w.tg.poll(a.backfill) + w.filings.poll(now() - timedelta(minutes=a.backfill)))
        w.save()
        log(f"once: {n} alerts · prices: {w.market.mode()}")
        return
    w.run(datetime.strptime(a.until, "%H:%M").time(), a.backfill)


if __name__ == "__main__":
    main()
