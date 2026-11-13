"""Live paper tracking of every suggestion, mistake tags, scorecards and learning.

Two kinds of paper trade, one lot each, real NSE option bid/ask at the moment the tracker sees the event:
  rule  the alert's trigger: after 9:30, first 15-min candle must close the alert's way, buy on a break of its
        high (CE) / low (PE) before 15:00, stop at the other side, exit 15:15. If the alert came when price was already
        more than 1% past the level, it waits for a pull-back to the level (as the alert says).
  tip   a tip channel's own levels: buy when the named option (or stock) trades at/above the tip's entry, exit at its
        target, its stop or 15:15.
Items the filters did not send are tracked too ("silent"), so the filters themselves get tested.
Learning: buckets (channel, news type, strength, side, time of day, how far price had run, model agreement) with
at least 30 closed trades and a clearly losing record get muted (alerts become silent); a muted bucket comes back when
its silent trades recover. Every change is reported.
"""

import json
import math
import os
from datetime import datetime, time as dtime, timedelta

from common import STATE, esc, log, now

SCORES = os.path.join(STATE, "scores.jsonl")
MIN_N, MUTE_PF, UNMUTE_PF = 30, 0.8, 1.0


def charges(buy, sell):
    """One buy and one sell of a stock option lot (brokerage, STT 0.15% on sell, exchange, GST, stamp)."""
    brok, ex = 40.0, 0.000355 * (buy + sell)
    return brok + 0.0015 * sell + ex + 0.000001 * (buy + sell) + 0.00003 * buy + 0.18 * (brok + ex)


def next_weekday(d):
    d += timedelta(days=1)
    while d.weekday() > 4:
        d += timedelta(days=1)
    return d


def bucket_keys(r):
    t = r.get("alert_hm", "00:00")
    tod = "pre-market/evening" if t < "09:15" or t >= "15:00" else "9:15-11:00" if t < "11:00" else "11:00-13:00" if t < "13:00" else "13:00-15:00"
    run = r.get("ran_atr")
    ran = "unknown" if run is None else "already ran >1 ATR" if run > 1 else "ran 0.5-1 ATR" if run > 0.5 else "fresh (<0.5 ATR)"
    keys = [f"channel: {r.get('source')}", f"news type: {r.get('event_type')}", f"strength: {r.get('strength')}",
            f"side: {r.get('side')}", f"alert time: {tod}", f"price had: {ran}", f"kind: {r.get('kind')}"]
    if r.get("model_agree") is not None:
        keys.append(f"daily model: {'agrees' if r['model_agree'] else 'disagrees'}")
    if r.get("kind") == "breakout":
        keys += [f"strike: {'out of the money' if 'out-of-the-money fallback' in r.get('tags', []) else 'in the money / ATM'}",
                 f"breakout type: {r.get('bo_kind')}", f"breakout score: {'6-7' if (r.get('bo_score') or 0) >= 6 else '4-5' if (r.get('bo_score') or 0) >= 4 else '0-3'}/7",
                 f"breakout source: {r.get('source')}"]
    return keys


class Tracker:
    def __init__(self, state, market, uni, budget, mode="auto"):
        self.state, self.m, self.uni, self.budget, self.mode = state, market, uni, budget, mode
        self.recs = state.setdefault("paper", [])
        self.muted = state.setdefault("muted", {})
        self.last_tip_poll = 0.0

    # ------------------------------------------------------------------ new records
    def add(self, sym, g, post, snap, opt, alerted, model_rank=None):
        side = "CE" if g["direction"] > 0 else "PE"
        today = now().date()
        t = now().time()
        plan_day = next_weekday(today) if t >= dtime(15, 0) else today
        kind = "tip" if g.get("event_type") == "tip/call" and g.get("tip") else "rule"
        rid = f"{plan_day}|{sym}|{side}|{kind}"
        if any(r["id"] == rid for r in self.recs):
            if alerted:
                for r in self.recs:
                    if r["id"] == rid and not r.get("alerted"):
                        r["alerted"] = True   # a silent record that later got alerted counts as alerted
            return
        spot, pc, atr = snap.get("spot"), snap.get("pc"), snap.get("atr")
        ran = abs(spot - pc) / atr if spot and pc and atr else None
        agree = None if model_rank is None else ((model_rank > 0.5) == (side == "CE"))
        r = dict(id=rid, day=str(plan_day), sym=sym, side=side, kind=kind, alerted=bool(alerted), status="waiting",
                 source=post["source"], event_type=g.get("event_type"), strength=g.get("strength"),
                 headline=g.get("headline"), alert_t=now().isoformat(), alert_hm=now().strftime("%H:%M"),
                 spot_at_alert=spot, ran_atr=ran, model_agree=agree, tip=g.get("tip") if kind == "tip" else None,
                 tags=[])
        if kind == "tip" and spot and r["tip"] and r["tip"].get("right") in (None, "STOCK", "FUT") and r["tip"].get("entry"):
            r["tip"]["on"] = "stock"
        self.recs.append(r)

    def add_breakout(self, sym, side, level, price, alerted, source, kind, score, start_next_day=False):
        today = now().date()
        day = next_weekday(today) if start_next_day else today
        opt_side = "CE" if side == "up" else "PE"
        rid = f"{day}|{sym}|{opt_side}|breakout"
        if any(r["id"] == rid for r in self.recs):
            return
        r = dict(id=rid, day=str(day), sym=sym, side=opt_side, kind="breakout", alerted=bool(alerted), status="waiting",
                 source=source, event_type=f"breakout ({kind})", strength=f"score {score}/7", headline=None,
                 alert_t=now().isoformat(), alert_hm=now().strftime("%H:%M"), spot_at_alert=price, ran_atr=None,
                 model_agree=None, tip=None, tags=[], level=float(level), bo_kind=kind, bo_score=int(score),
                 hold_days=3, days_held=0)
        self.recs.append(r)

    def _breakout(self, r, t):
        if t.time() < dtime(9, 30):
            return
        bars = self.m.series(r["sym"])
        if not bars or bars[-1][0].date() != t.date():
            return
        px = bars[-1][3]
        up = r["side"] == "CE"
        if r["status"] == "waiting":
            if r["day"] != str(t.date()):
                return
            beyond = px > r["level"] * 1.005 if up else px < r["level"] * 0.995
            if beyond:
                self._enter(r, px, bars[-1][0])
                r["last_day"] = str(t.date())
            elif t.time() > dtime(10, 30):
                r.update(status="no_trigger", tags=r["tags"] + ["fell back behind the line before entry"])
                self._score(r)
            return
        if r["status"] == "open":
            if r.get("last_day") != str(t.date()):
                r["days_held"] = r.get("days_held", 0) + 1
                r["last_day"] = str(t.date())
            et = datetime.fromisoformat(r["entry_t"])
            et = et.replace(tzinfo=None) if et.tzinfo else et
            for b in bars:
                if b[0] <= et:
                    continue
                mv = (b[3] / r["entry_spot"] - 1) * (1 if up else -1)
                r["best_move"] = max(r.get("best_move", 0), mv)
                back = b[3] < r["level"] * 0.99 if up else b[3] > r["level"] * 1.01
                if back:
                    self._exit(r, b[3], b[0], "stop")
                    return
                if r["days_held"] >= r.get("hold_days", 3) - 1 and b[0].time() >= dtime(15, 15):
                    self._exit(r, b[3], b[0], "time")
                    return

    # ------------------------------------------------------------------ evaluation
    def _bars(self, sym, day_str):
        if day_str != str(now().date()):
            return []
        return self.m.series(sym)

    def _enter(self, r, spot, when):
        opt = self.m.pick_option(r["sym"], r["side"], self.budget)
        if not opt.get("strike") and opt.get("otm"):
            o = opt["otm"]   # the alert's "nearest affordable" strike: track it so OTM gets judged too
            opt = dict(opt, strike=o["strike"], ask=o["ask"], px=o["px"], delta=o["delta"])
            r["tags"].append("out-of-the-money fallback")
        if not opt.get("strike"):
            r.update(status="no_option", tags=r["tags"] + ["no strike within budget"])
            return
        r.update(status="open", entry_t=when.isoformat(), entry_spot=spot, strike=opt["strike"], expiry=str(opt["expiry"]),
                 lot=opt["lot"], entry_opt=opt["ask"] or opt["px"], delta=opt["delta"])

    def _exit(self, r, spot, when, reason):
        q = self.m.option_quote(r["sym"], datetime.fromisoformat(r["expiry"]).date() if "T" in r["expiry"] else
                                datetime.strptime(r["expiry"], "%Y-%m-%d").date(), r["strike"], r["side"])
        px = (q or {}).get("bid") or (q or {}).get("ltp")
        if not px:
            move = (spot / r["entry_spot"] - 1) * (1 if r["side"] == "CE" else -1)
            px = max(r["entry_opt"] * (1 + 18 * move - 0.02), 0.05)   # fallback: real-option fit for this strike type
            r["tags"].append("exit price estimated")
        buy, sell = r["entry_opt"] * r["lot"], px * r["lot"]
        pnl = sell - buy - charges(buy, sell)
        r.update(status="closed", exit_t=when.isoformat(), exit_spot=spot, exit_opt=px, exit_reason=reason,
                 pnl=round(pnl, 0), ret=round(pnl / buy, 4) if buy else None)
        hold = (when - datetime.fromisoformat(r["entry_t"])).total_seconds() / 60
        if pnl < 0:
            if reason == "stop" and hold <= 30:
                r["tags"].append("false breakout (stopped within 30 min)")
            if reason == "time" and r.get("best_move", 0) > 0.005:
                r["tags"].append("faded (was up, gave it back)")
            if (r.get("ran_atr") or 0) > 1:
                r["tags"].append("late (price had already run > 1 ATR)")
            if r.get("kind") == "breakout" and reason == "stop":
                r["tags"].append("failed breakout (back behind the line)")
        self._score(r)

    def _rule(self, r, t):
        bars = self._bars(r["sym"], r["day"])
        if not bars:
            return
        up = r["side"] == "CE"
        first = [b for b in bars if dtime(9, 15) <= b[0].time() < dtime(9, 30)]
        if not first or t.time() < dtime(9, 30):
            return
        oh, ol = max(b[1] for b in first), min(b[2] for b in first)
        trig, stop = (oh, ol) if up else (ol, oh)
        if r["status"] == "waiting":
            if (first[-1][3] > first[0][3]) != up:
                r.update(status="skipped", tags=r["tags"] + ["first 15-min candle against (rule skip)"])
                self._score(r)
                return
            start = max(datetime.fromisoformat(r["alert_t"]).replace(tzinfo=None), datetime.combine(t.date(), dtime(9, 30)))
            late = r.get("spot_at_alert") and abs(r["spot_at_alert"] / trig - 1) > 0.01 and \
                ((r["spot_at_alert"] > trig) if up else (r["spot_at_alert"] < trig)) and r["alert_hm"] >= "09:30" and r["day"] == r["alert_t"][:10]
            for b in bars:
                if b[0] < start or b[0].time() > dtime(15, 0):
                    continue
                hit = (b[2] <= trig) if late else ((b[1] >= trig) if up else (b[2] <= trig))
                if late and not up:
                    hit = b[1] >= trig
                if hit:
                    self._enter(r, b[3], b[0])
                    if late:
                        r["tags"].append("entered on pull-back")
                    break
            if r["status"] == "waiting" and t.time() > dtime(15, 0):
                r.update(status="no_trigger", tags=r["tags"] + ["never triggered"])
                self._score(r)
            return
        if r["status"] == "open":
            et = datetime.fromisoformat(r["entry_t"])
            et = et.replace(tzinfo=None) if et.tzinfo else et
            for b in bars:
                if b[0] <= et:
                    continue
                mv = (b[3] / r["entry_spot"] - 1) * (1 if up else -1)
                r["best_move"] = max(r.get("best_move", 0), mv)
                if (b[2] <= stop) if up else (b[1] >= stop):
                    self._exit(r, b[3], b[0], "stop")
                    return
                if b[0].time() >= dtime(15, 15):
                    self._exit(r, b[3], b[0], "time")
                    return

    def _tip(self, r, t):
        tip = r["tip"]
        if tip.get("on") == "stock":
            bars = self._bars(r["sym"], r["day"])
            px = bars[-1][3] if bars else None
        else:
            if not tip.get("strike") or tip.get("right") not in ("CE", "PE"):
                r.update(status="skipped", tags=r["tags"] + ["tip without clear option/levels"])
                self._score(r)
                return
            if not r.get("expiry"):
                e = self.m.nearest_expiry(r["sym"])
                if not e:
                    return
                r["expiry"] = str(e)
            q = self.m.option_quote(r["sym"], datetime.strptime(r["expiry"], "%Y-%m-%d").date(), tip["strike"], tip["right"])
            px = (q or {}).get("ltp")
        if not px:
            return
        sgn = -1 if (tip.get("on") == "stock" and r["side"] == "PE") else 1   # bearish stock tip: "sell below"
        above = lambda a, b: (a - b) * sgn >= 0
        if r["status"] == "waiting":
            if tip.get("entry") is None or above(px, tip["entry"]):
                if tip.get("entry") and abs(px / tip["entry"] - 1) > 0.03 and r["alert_hm"] == t.strftime("%H:%M"):
                    r["tags"].append("tip posted after its entry level had passed")
                if tip.get("on") == "stock":
                    self._enter(r, px, t.replace(tzinfo=None))
                else:
                    r.update(status="open", entry_t=t.replace(tzinfo=None).isoformat(), entry_spot=px, strike=tip["strike"],
                             lot=(self.uni.get(r["sym"]) or {}).get("lot"), entry_opt=px, delta=None)
            elif t.time() > dtime(15, 0):
                r.update(status="no_trigger", tags=r["tags"] + ["tip entry never reached"])
                self._score(r)
            return
        if r["status"] == "open":
            reason = "target" if tip.get("target") and above(px, tip["target"]) else \
                "stop" if tip.get("stop") and above(tip["stop"], px) else "time" if t.time() >= dtime(15, 15) else None
            if reason:
                if tip.get("on") == "stock":
                    self._exit(r, px, t.replace(tzinfo=None), reason)
                else:
                    q = self.m.option_quote(r["sym"], datetime.strptime(r["expiry"], "%Y-%m-%d").date(), tip["strike"], tip["right"])
                    out = (q or {}).get("bid") or px
                    lot = r.get("lot") or 0
                    buy, sell = r["entry_opt"] * lot, out * lot
                    pnl = sell - buy - charges(buy, sell)
                    r.update(status="closed", exit_t=t.replace(tzinfo=None).isoformat(), exit_opt=out, exit_reason=reason,
                             pnl=round(pnl, 0), ret=round(pnl / buy, 4) if buy else None)
                    self._score(r)

    def update(self):
        t = now()
        today = str(t.date())
        tip_round = (t.timestamp() - self.last_tip_poll) >= 290
        for r in self.recs:
            if r["status"] not in ("waiting", "open"):
                continue
            if r["kind"] == "breakout":
                try:
                    self._breakout(r, t)
                except Exception as e:
                    log(f"tracker {r['id']}: {str(e)[:80]}")
                continue
            if r["day"] != today:
                continue
            try:
                if r["kind"] == "tip":
                    if tip_round:
                        self._tip(r, t)
                else:
                    self._rule(r, t)
            except Exception as e:
                log(f"tracker {r['id']}: {str(e)[:80]}")
        if tip_round:
            self.last_tip_poll = t.timestamp()

    def close_day(self):
        """At 15:16: anything still open exits at the current price; still-waiting rule trades never triggered."""
        t = now()
        for r in self.recs:
            if r["kind"] == "breakout":
                if r["status"] == "waiting" and r["day"] == str(t.date()):
                    r.update(status="no_trigger", tags=r["tags"] + ["never triggered"])
                    self._score(r)
                continue
            if r["day"] != str(t.date()):
                continue
            if r["status"] == "open":
                bars = self._bars(r["sym"], r["day"])
                spot = bars[-1][3] if bars else r["entry_spot"]
                if r["kind"] == "tip" and r["tip"].get("on") != "stock":
                    self._tip(r, t)
                    if r["status"] == "open":
                        r["status"] = "closed"
                        self._score(r)
                else:
                    self._exit(r, spot, t.replace(tzinfo=None), "time")
            elif r["status"] == "waiting":
                r.update(status="no_trigger", tags=r["tags"] + ["never triggered" if r["kind"] == "rule" else "tip entry never reached"])
                self._score(r)

    def roll(self):
        """Keep only today's and future records in the live state (closed ones are in scores.jsonl)."""
        today = str(now().date())
        self.recs[:] = [r for r in self.recs if r["day"] >= today and r["status"] in ("waiting", "open")
                        or r["day"] == today or (r["kind"] == "breakout" and r["status"] == "open")]
        self.state["paper"] = self.recs

    def _score(self, r):
        os.makedirs(STATE, exist_ok=True)
        with open(SCORES, "a") as f:
            f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ reports
    def apply_choice(self, kind, n):
        """Owner approved suggestion number n from the last report (kind 'mute' or 'unmute')."""
        lst = self.state.get(f"suggest_{kind}") or []
        if not (1 <= n <= len(lst)):
            return f"No suggestion number {n}. Send /learn to see the current list."
        k = lst[n - 1]
        if kind == "mute":
            self.muted[k] = dict(since=str(now().date()), by="owner")
            return f"🔇 Muted {esc(k)}. Its alerts are now tracked silently; /unmute brings it back when suggested."
        self.muted.pop(k, None)
        return f"🔊 Un-muted {esc(k)}. Its alerts will be sent again."

    def muted_for(self, like):
        """Muted groups this would-be alert belongs to (like: dict with the same fields as a record)."""
        return [k for k in bucket_keys(like) if k in self.muted]

    def scorecard(self):
        today = str(now().date())
        rows = [r for r in self.recs if r["day"] == today]
        done = [r for r in rows if r["status"] == "closed" and r.get("pnl") is not None]
        al = [r for r in done if r["alerted"]]
        sil = [r for r in done if not r["alerted"]]
        lines = [f"📊 <b>Paper scorecard {today}</b> — {len(rows)} suggestions tracked (1 lot each, real NSE bid/ask)"]
        for name, grp in (("Sent alerts", al), ("Silent (filtered) items", sil)):
            if grp:
                w = sum(1 for r in grp if r["pnl"] > 0)
                lines.append(f"<b>{name}</b>: {len(grp)} trades · {w} won · total ₹{sum(r['pnl'] for r in grp):+,.0f}")
        for r in sorted(al, key=lambda r: -abs(r["pnl"]))[:12]:
            lines.append(f"{'✅' if r['pnl'] > 0 else '❌'} {esc(r['sym'])} {r['side']} {r['kind']} · ₹{r['pnl']:+,.0f} "
                         f"({r['exit_reason']}){' · ' + esc('; '.join(r['tags'])) if r['tags'] else ''}")
        other = [r for r in rows if r["status"] in ("skipped", "no_trigger", "no_option")]
        if other:
            from collections import Counter
            c = Counter(r["status"] for r in other)
            lines.append("No trade: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in c.items()))
        if len(lines) == 1:
            lines.append("Nothing to score yet today.")
        return "\n".join(lines)

    def history(self):
        if not os.path.exists(SCORES):
            return []
        out = {}
        for line in open(SCORES):
            try:
                r = json.loads(line)
                out[r["id"]] = r          # last write per trade wins
            except (ValueError, KeyError):
                continue
        return list(out.values())

    def learn(self, apply=True):
        rows = [r for r in self.history() if r.get("status") == "closed" and r.get("pnl") is not None]
        lines = [f"🧠 <b>What the paper trades teach</b> — {len(rows)} closed trades so far"]
        if not rows:
            lines.append("No closed trades yet. Lessons start after a few days of alerts.")
            return "\n".join(lines)
        def stats(g):
            p = sum(r["pnl"] for r in g if r["pnl"] > 0)
            q = -sum(r["pnl"] for r in g if r["pnl"] < 0)
            rets = [r.get("ret") or 0 for r in g]
            mu = sum(rets) / len(rets)
            sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / max(len(rets) - 1, 1))
            return dict(n=len(g), win=sum(1 for r in g if r["pnl"] > 0) / len(g), pf=p / q if q else float("inf"),
                        total=sum(r["pnl"] for r in g), t=mu / sd * math.sqrt(len(g)) if sd else 0.0)
        allst = stats(rows)
        lines.append(f"All: win {allst['win'] * 100:.0f}% · PF {allst['pf']:.2f} · total ₹{allst['total']:+,.0f}")
        buckets = {}
        for r in rows:
            for k in bucket_keys(r):
                buckets.setdefault(k, []).append(r)
        ranked = sorted(((k, stats(g)) for k, g in buckets.items() if len(g) >= 8), key=lambda kv: kv[1]["pf"])
        if ranked:
            lines.append("<b>Worst</b>: " + "; ".join(f"{esc(k)} (n {s['n']}, PF {s['pf']:.2f})" for k, s in ranked[:4]))
            lines.append("<b>Best</b>: " + "; ".join(f"{esc(k)} (n {s['n']}, PF {s['pf']:.2f})" for k, s in ranked[::-1][:4]))
        from collections import Counter
        tags = Counter(t for r in self.history() for t in r.get("tags", []))
        if tags:
            lines.append("<b>Common mistakes</b>: " + "; ".join(f"{esc(t)} ×{n}" for t, n in tags.most_common(5)))
        changes, mute_s, unmute_s = [], [], []
        for k, g in buckets.items():
            s = stats(g)
            if k not in self.muted and s["n"] >= MIN_N and s["pf"] < MUTE_PF and s["t"] < -1:
                mute_s.append((k, s))
            elif k in self.muted:
                recent = [r for r in g if r["day"] >= self.muted[k]["since"] and not r["alerted"]][-MIN_N:]
                if len(recent) >= MIN_N and stats(recent)["pf"] >= UNMUTE_PF:
                    unmute_s.append((k, stats(recent)))
        if apply and self.mode == "auto":
            for k, s in mute_s:
                self.muted[k] = dict(since=str(now().date()), n=s["n"], pf=round(s["pf"], 2))
                changes.append(f"🔇 muted {esc(k)} (n {s['n']}, PF {s['pf']:.2f}) — tracked silently from now")
            for k, s in unmute_s:
                del self.muted[k]
                changes.append(f"🔊 un-muted {esc(k)} (silent record recovered)")
        else:
            # suggest only: numbered so the owner can approve with /mute N or /unmute N
            self.state["suggest_mute"] = [k for k, _ in mute_s]
            self.state["suggest_unmute"] = [k for k, _ in unmute_s]
            for i, (k, s) in enumerate(mute_s, 1):
                changes.append(f"💡 Suggest muting <b>{esc(k)}</b> (n {s['n']}, PF {s['pf']:.2f}) — reply /mute {i}")
            for i, (k, s) in enumerate(unmute_s, 1):
                changes.append(f"💡 Suggest un-muting <b>{esc(k)}</b> (silent PF {s['pf']:.2f}) — reply /unmute {i}")
        if changes:
            lines += changes
        if self.muted:
            lines.append("Muted now: " + "; ".join(esc(k) for k in self.muted))
        lines.append(f"<i>A group is suggested for muting after {MIN_N}+ closed trades with PF below {MUTE_PF}; "
                     f"muted groups keep being tracked silently.</i>")
        return "\n".join(lines)
