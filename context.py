"""Stock context added to every alert: chart checks, F&O ban, results date, last quarter's numbers.

All from free NSE data:
- chart: the daily-price store kept for the breakout scan (state/bhav), with today's live price on top
- F&O ban: nsearchives fo_secban_<DDMMYYYY>.csv (published the evening before)
- results dates: NSE board-meeting list (purpose "Financial Results")
- last quarter: NSE integrated filings (XBRL) - revenue and profit for the latest quarter and a year earlier
"""

import math
import os
import re
import urllib.request
from datetime import datetime, timedelta

import numpy as np

import breakouts as BO
from common import STATE, Nse, esc, load_json, log, next_trading_day, now, save_json, trading_day

CACHE = os.path.join(STATE, "context.json")
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"}


def crore(x):
    cr = x / 1e7
    if abs(cr) >= 1e5:
        return f"₹{cr / 1e5:,.2f} L cr"
    return f"₹{cr:,.0f} cr" if abs(cr) >= 100 else f"₹{cr:,.1f} cr"


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1) * (1 if b > 0 else -1)


class Context:
    def __init__(self, store_fn):
        self.store_fn = store_fn          # returns the daily-price store (cached by the caller)
        self.nse = Nse()
        self.c = load_json(CACHE, {})
        self.c.setdefault("ban", {})
        self.c.setdefault("fund", {})

    def save(self):
        save_json(CACHE, self.c)

    # ------------------------------------------------------------------ F&O ban
    def banned(self, day):
        """Set of symbols in F&O ban on `day`, or None if NSE has not published that day's list yet."""
        key = str(day)
        hit = self.c["ban"].get(key)
        if hit is not None and (hit.get("syms") is not None or now().timestamp() - hit.get("t", 0) < 1800):
            return None if hit.get("syms") is None else set(hit["syms"])
        syms = None
        try:
            req = urllib.request.Request(f"https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{day:%d%m%Y}.csv",
                                         headers=UA)
            txt = urllib.request.urlopen(req, timeout=20).read().decode(errors="ignore")
            syms = sorted({ln.split(",", 1)[1].strip().upper() for ln in txt.splitlines()[1:] if "," in ln})
        except Exception:
            pass   # 404 until NSE publishes it; retried every 30 minutes
        self.c["ban"] = {k: v for k, v in self.c["ban"].items() if k >= str(now().date() - timedelta(days=5))}
        self.c["ban"][key] = {"t": now().timestamp(), "syms": syms}
        self.save()
        return None if syms is None else set(syms)

    # ------------------------------------------------------------------ results dates
    def results_dates(self):
        """{sym: date} of the next 'Financial Results' board meeting within ~3 weeks (refreshed every 6 hours)."""
        rd = self.c.get("results") or {}
        if now().timestamp() - rd.get("t", 0) > 6 * 3600:
            d0, d1 = now().date() - timedelta(days=1), now().date() + timedelta(days=21)
            try:
                rows = self.nse.api(f"/api/corporate-board-meetings?index=equities&from_date={d0:%d-%m-%Y}&to_date={d1:%d-%m-%Y}")
                out = {}
                for r in rows or []:
                    if "result" not in f"{r.get('bm_purpose', '')} {r.get('bm_desc', '')}".lower():
                        continue
                    try:
                        d = datetime.strptime(r["bm_date"], "%d-%b-%Y").date()
                    except (KeyError, ValueError):
                        continue
                    s = (r.get("bm_symbol") or "").upper()
                    if s and (s not in out or d < datetime.fromisoformat(out[s]).date()):
                        out[s] = str(d)
                rd = {"t": now().timestamp(), "dates": out}
                self.c["results"] = rd
                self.save()
            except Exception as e:
                log(f"board meetings unavailable: {str(e)[:80]}")
                rd = dict(rd, t=now().timestamp() - 5 * 3600)   # retry in an hour
                self.c["results"] = rd
        return {s: datetime.fromisoformat(d).date() for s, d in (rd.get("dates") or {}).items()}

    # ------------------------------------------------------------------ last quarter's numbers
    def _xbrl_numbers(self, url, qe):
        req = urllib.request.Request(url, headers=UA)
        x = urllib.request.urlopen(req, timeout=40).read().decode(errors="ignore")
        quarter_ctx = set()
        for cid, body in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', x, re.S):
            if "explicitMember" in body:
                continue
            p = re.findall(r"<xbrli:startDate>([^<]+)</xbrli:startDate>\s*<xbrli:endDate>([^<]+)<", body)
            if p and p[0][1] == f"{qe:%Y-%m-%d}" and 80 <= (qe - datetime.fromisoformat(p[0][0]).date()).days <= 100:
                quarter_ctx.add(cid)

        def first(tags):
            for tag in tags:
                for cref, val in re.findall(rf'<[\w-]+:{tag} contextRef="([^"]+)"[^>]*>([^<]+)<', x):
                    if cref in quarter_ctx:
                        try:
                            return float(val)
                        except ValueError:
                            pass
            return None
        sales = first(("RevenueFromOperations", "InterestEarned", "Income", "TotalIncome"))
        profit = first(("ProfitOrLossAttributableToOwnersOfParent", "ProfitLossForPeriod", "ProfitLossForThePeriod",
                        "NetProfitLossForThePeriod", "ProfitLossFromOrdinaryActivitiesAfterTax"))
        return sales, profit

    def quarter(self, sym):
        """Latest quarter's revenue and profit with year-on-year change (cached a week per stock)."""
        hit = self.c["fund"].get(sym)
        if hit and now().timestamp() - hit.get("t", 0) < 7 * 86400:
            return hit.get("q")
        q = None
        try:
            rows = (self.nse.api(f"/api/integrated-filing-results?symbol={sym}") or {}).get("data", [])
            fin = {}
            for r in rows:
                if "financ" not in (r.get("type") or "").lower() or not r.get("xbrl", "").endswith(".xml"):
                    continue
                try:
                    qe = datetime.strptime(r["qe_Date"], "%d-%b-%Y").date()
                except (KeyError, ValueError):
                    continue
                k = (qe, r.get("consolidated") or "")
                if k not in fin or (r.get("broadcast_Date") or "") > (fin[k].get("broadcast_Date") or ""):
                    fin[k] = r
            if fin:
                latest = max(qe for qe, _ in fin)
                basis = "Consolidated" if (latest, "Consolidated") in fin else "Standalone"
                cur = fin.get((latest, basis))
                ago_qe = latest.replace(year=latest.year - 1)
                ago = fin.get((ago_qe, basis))
                s1, p1 = self._xbrl_numbers(cur["xbrl"], latest)
                s0, p0 = self._xbrl_numbers(ago["xbrl"], ago_qe) if ago else (None, None)
                if s1 is not None or p1 is not None:
                    q = dict(qe=str(latest), basis=basis, sales=s1, profit=p1, sales_yoy=pct(s1, s0), profit_yoy=pct(p1, p0),
                             loss_to_profit=bool(p0 is not None and p1 is not None and p0 < 0 < p1),
                             profit_to_loss=bool(p0 is not None and p1 is not None and p1 < 0 < p0))
        except Exception as e:
            log(f"quarterly numbers for {sym}: {str(e)[:80]}")
            return None   # not cached: try again next time
        self.c["fund"][sym] = {"t": now().timestamp(), "q": q}
        self.save()
        return q

    # ------------------------------------------------------------------ chart checks
    def chart(self, sym, side, px, asof=None):
        """Five checks of whether the daily chart backs a CE (side 'CE') or PE trade at price px (priced on `asof`)."""
        s = (self.store_fn() or {}).get(sym)
        if not s or len(s["c"]) < 60 or not px:
            return [], 0, {}
        up = side == "CE"
        c, h, l = s["c"], s["h"], s["l"]
        live = str(s["days"][-1]) != str(asof or now().date())   # store ends before the price's day: put it on top
        cc = np.append(c, px) if live else c.copy()
        e20, e50, e200 = (BO.ema(cc, n)[-1] for n in (20, 50, 200))
        r = BO.rsi(cc)[-1]
        a = BO.atr(h, l, c)[-1]
        prev = c[-1] if live else c[-2]
        move = (px - prev) / a if a else float("nan")
        piv = BO.pivots(h) if up else BO.pivots(l, high=False)
        lvls = [h[p] for p in piv if h[p] > px * 1.003] if up else [l[p] for p in piv if l[p] < px * 0.997]
        room = ((min(lvls) / px - 1) if up else (1 - max(lvls) / px)) if lvls else None
        yr_h, yr_l = max(h[-250:].max(), px), min(l[-250:].min(), px)
        ran = move if up else -move                       # how far it has already gone the trade's way
        when = "today's" if not asof or str(asof) == str(now().date()) else f"{asof:%a}'s"
        out = [
            ((px > e20 and px > e50) if up else (px < e20 and px < e50),
             ("above" if px > e20 and px > e50 else "below" if px < e20 and px < e50 else "between") + " 20/50 EMA"),
            ((px > e200) if up else (px < e200), ("above" if px > e200 else "below") + " 200 EMA"),
            ((r < 70) if up else (r > 30), f"RSI {r:.0f}" + (" stretched" if (r >= 70 if up else r <= 30) else "")),
            ((room is None or room >= 0.02),
             ("no resistance above (blue sky)" if up else "no support below") if room is None
             else f"{'resistance' if up else 'support'} {room * 100:.1f}% {'above' if up else 'below'}"),
            ((math.isnan(ran) or ran <= 1), f"{when} move ?" if math.isnan(ran) else
             (f"already ran {ran:.1f}× a normal day" if ran > 1 else f"{when} move {move:+.1f}× a normal day")),
        ]
        info = dict(from_52w_high=px / yr_h - 1, from_52w_low=px / yr_l - 1)
        return out, sum(1 for ok, _ in out if ok), info

    # ------------------------------------------------------------------ the alert block
    def block(self, sym, side, px, plan_day=None, chart=True, fundamentals=True, asof=None):
        """Lines for the alert plus facts for the paper tracker."""
        plan_day = plan_day or (now().date() if trading_day() and now().hour < 15 else next_trading_day())
        lines, facts = [], {}
        try:
            checks, score, info = self.chart(sym, side, px, asof) if chart else ([], 0, {})
        except Exception as e:
            log(f"chart checks for {sym}: {str(e)[:80]}")
            checks, score, info = [], 0, {}
        if checks:
            word = "agrees" if score >= 4 else "mixed" if score == 3 else "disagrees"
            hi = info["from_52w_high"]
            yr = " · at a 52-week high" if hi >= -0.005 else f" · {abs(hi) * 100:.0f}% below 52-week high" if hi > -0.15 else ""
            lines.append(f"📐 <b>Chart {word} {score}/5</b>: " + " · ".join(f"{'✅' if ok else '⚠️'} {esc(t)}" for ok, t in checks) + yr)
            facts.update(chart_score=score)
        try:
            ban = self.banned(plan_day)
        except Exception:
            ban = None
        if ban is not None and sym in ban:
            lines.append(f"⛔ <b>In F&amp;O ban on {plan_day:%a %d %b}</b> — no new F&amp;O positions allowed (closing only).")
            facts.update(ban=True)
        try:
            rd = self.results_dates().get(sym)
        except Exception:
            rd = None
        if rd and rd >= now().date():
            days = (rd - now().date()).days
            when = "today" if days == 0 else "tomorrow" if days == 1 else f"on {rd:%a %d %b}"
            if days <= 10:
                lines.append(f"⚠️ <b>Results {when}</b> — option prices are inflated before results and drop right after "
                             f"(IV crush): a right call can still lose. Exit before the results or skip.")
            facts.update(results_in_days=days)
        try:
            q = self.quarter(sym) if fundamentals else None
        except Exception:
            q = None
        if q:
            qe = datetime.fromisoformat(q["qe"]).date()

            def part(name, v, g):
                if v is None:
                    return None
                gtxt = "" if g is None else f" ({g * 100:+.0f}% YoY)"
                return f"{name} {crore(v)}{gtxt}"
            bits = [b for b in (part("sales", q["sales"], q["sales_yoy"]), part("profit", q["profit"], q["profit_yoy"])) if b]
            if q.get("loss_to_profit"):
                bits.append("turned profitable")
            if q.get("profit_to_loss"):
                bits.append("swung to a loss")
            if bits:
                lines.append(f"📊 {qe:%b-%y} quarter: " + " · ".join(bits))
            facts.update(profit_yoy=q.get("profit_yoy"), sales_yoy=q.get("sales_yoy"))
        return lines, facts
