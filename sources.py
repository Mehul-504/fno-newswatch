"""News sources: the user's Telegram channels (read-only) and NSE corporate filings.

Telegram safety: only reads channels the account has already joined. It never sends, joins, leaves, deletes or marks
anything as read (no send_message, no read acknowledgement). Photos with short captions are downloaded and their text
read by local OCR (Tesseract, or macOS Vision on the Mac).
"""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile

from common import LOCAL_FNO, Nse, log, now, secret

ROUTINE = {
    "Analysts/Institutional Investor Meet/Con. Call Updates", "Copy of Newspaper Publication", "Investor Presentation",
    "ESOP/ESOS/ESPS", "Record Date", "Shareholders meeting", "Dividend",
    "Certificate under SEBI (Depositories and Participants) Regulations, 2018", "Monitoring Agency Report",
    "Statement of deviation(s) or variation(s) under Reg. 32", "Trading Window", "Committee Meeting Updates",
    "Change in Company Secretary/Compliance Officer", "Change in Auditors", "Change in Director(s)", "Appointment",
    "Retirement", "Cessation", "Options to purchase securities", "Giving guarantees/indemnity/ becoming a surety for third party",
    "Increase in Authorised Capital", "Name and Symbol Change", "Related Party Transactions", "Allotment of Securities",
    "Board Meeting Intimation", "Notice Of Shareholders Meetings", "Postal Ballot", "Loss of Share Certificates",
    "Duplicate Share Certificate", "Updates", "Credit Rating",
}
NOISE = ("SECURITIES IN BAN", "SLBM", "DII DATA", "FII DATA", "GLOBAL CUES", "OPEN INTEREST GAINERS")
MAC_OCR = os.path.join(LOCAL_FNO, "stock_fno", "ocr_bin")


def ocr(path):
    try:
        if shutil.which("tesseract"):
            out = subprocess.run(["tesseract", path, "stdout", "--psm", "4"], capture_output=True, text=True, timeout=60)
            return out.stdout.strip()
        if os.path.exists(MAC_OCR):
            import json
            out = subprocess.run([MAC_OCR, path], capture_output=True, text=True, timeout=60)
            return json.loads(out.stdout or "{}").get("text", "").strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return ""


class Telegram:
    def __init__(self, channels, state, ocr_per_day=300):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        sess = secret("TG_SESSION")
        session = StringSession(sess) if sess else os.path.join(LOCAL_FNO, "telegram")
        self.client = TelegramClient(session, int(secret("TELEGRAM_API_ID")), secret("TELEGRAM_API_HASH"))
        self.wanted = channels
        self.state = state.setdefault("tg_last", {})
        self.ocr_left = ocr_per_day
        self.tmp = tempfile.mkdtemp(prefix="nw_tg_")
        self.ents = {}

    def start(self):
        return self.loop.run_until_complete(self._start())

    async def _start(self):
        from telethon.tl.types import Channel
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not logged in")
        want = {w.lower().lstrip("@") for w in self.wanted}
        async for d in self.client.iter_dialogs():
            e = d.entity
            if isinstance(e, Channel):
                u, t = (e.username or "").lower(), (d.name or "").strip().lower()
                if u in want or t in want:
                    self.ents[e.id] = (e, d.name)
        missing = want - {(e.username or "").lower() for e, _ in self.ents.values()} - {n.strip().lower() for _, n in self.ents.values()}
        log(f"telegram: reading {len(self.ents)} channels" + (f"; not joined/not found: {sorted(missing)}" if missing else ""))
        return [n for _, n in self.ents.values()]

    def poll(self, backfill_minutes=0):
        try:
            return self.loop.run_until_complete(self._poll(backfill_minutes))
        except Exception as e:  # network blips: reconnect next round
            log(f"telegram poll error: {str(e)[:100]}")
            try:
                self.loop.run_until_complete(self.client.connect())
            except Exception:
                pass
            return []

    async def _poll(self, backfill_minutes):
        from datetime import timedelta
        from telethon.tl.types import MessageMediaPhoto
        out = []
        cutoff = now() - timedelta(minutes=backfill_minutes)
        for cid, (ent, title) in self.ents.items():
            key = str(cid)
            last = int(self.state.get(key, 0))
            msgs = await self.client.get_messages(ent, limit=40, min_id=last)
            if not last:   # first look at this channel: remember where it is, only report the backfill window
                if msgs:
                    self.state[key] = max(m.id for m in msgs)
                msgs = [m for m in msgs if m.date and m.date.astimezone(cutoff.tzinfo) >= cutoff] if backfill_minutes else []
            for m in sorted(msgs, key=lambda m: m.id):
                self.state[key] = max(int(self.state.get(key, 0)), m.id)
                text = m.message or ""
                if isinstance(m.media, MessageMediaPhoto) and len(text) < 200 and self.ocr_left > 0:
                    self.ocr_left -= 1
                    try:
                        p = await self.client.download_media(m, file=os.path.join(self.tmp, f"{cid}_{m.id}.jpg"))
                        if p:
                            img = ocr(p)
                            os.remove(p)
                            if img:
                                text = (text + "\n[image text] " + img).strip()
                    except Exception:
                        pass
                if not text.strip() or any(n in text.upper() for n in NOISE):
                    continue
                out.append(dict(source=title, kind="telegram", id=f"tg{cid}_{m.id}",
                                ts=m.date.astimezone(now().tzinfo), text=text))
            await asyncio.sleep(0.8)
        return out


class Filings:
    """NSE corporate announcements for F&O stocks, routine filings skipped."""

    def __init__(self, fno, state):
        self.nse = Nse()
        self.fno = fno
        self.seen = set(state.setdefault("nse_seen", []))
        self.state = state

    def poll(self, since=None):
        from datetime import datetime
        out = []
        try:
            rows = self.nse.announcements()
        except Exception as e:
            log(f"NSE filings error: {str(e)[:80]}")
            return out
        for x in rows:
            sid, sym = str(x.get("seq_id") or x.get("an_dt")), x.get("symbol")
            if sid in self.seen or sym not in self.fno:
                continue
            self.seen.add(sid)
            desc = (x.get("desc") or "").strip()
            if desc in ROUTINE:
                continue
            try:
                ts = datetime.strptime(x["an_dt"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=now().tzinfo)
            except (KeyError, ValueError):
                ts = now()
            if since is not None and ts < since:
                continue   # first look of the day: older filings are only marked as seen
            text = f"{desc}: {re.sub(r'\s+', ' ', x.get('attchmntText') or '')}"[:900]
            out.append(dict(source="NSE filing", kind="filing", id=f"nse{sid}", ts=ts, text=text, syms=[sym]))
        self.state["nse_seen"] = sorted(self.seen)[-3000:]
        return out
