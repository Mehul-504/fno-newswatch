"""Prices for alerts, best source first: Breeze (if today's session works) -> NSE website (no login) -> stored levels.

snapshot(): spot, change vs previous close, today's 15-minute opening range, day high/low, source.
pick_option(): monthly expiry with >= 7 days left; the deepest in-the-money strike (delta up to ~0.75) whose lot costs
<= the budget at the current ask, else ATM if it fits; with bid/ask spread.
"""

import math
import time
from datetime import datetime, time as dtime, timedelta

from common import Nse, log, now, secret


def bs_delta(spot, strike, days, iv_pct, right, r=0.065):
    if spot <= 0 or strike <= 0 or days <= 0 or iv_pct <= 0:
        return 1.0 if (right == "CE" and spot > strike) or (right == "PE" and spot < strike) else 0.0
    t, v = days / 365, iv_pct / 100
    d1 = (math.log(spot / strike) + (r + v * v / 2) * t) / (v * math.sqrt(t))
    nd = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return nd if right == "CE" else nd - 1


class Market:
    def __init__(self, uni, levels):
        self.uni, self.levels = uni, levels
        self.nse = Nse(gap=1.0)
        self.breeze, self.breeze_note = None, "no Breeze session"
        self.cache = {}
        self.nse_ok = None

    # -------------------------------------------------------------- Breeze
    def connect_breeze(self, token):
        if not token:
            return False
        try:
            from breeze_connect import BreezeConnect
            b = BreezeConnect(api_key=secret("BREEZE_API_KEY"))
            b.generate_session(api_secret=secret("BREEZE_API_SECRET"), session_token=str(token).strip())
            d = now().date()
            r = b.get_historical_data_v2(interval="1day", from_date=f"{d - timedelta(days=7)}T09:15:00.000Z",
                                         to_date=f"{d}T15:30:00.000Z", stock_code="NIFTY", exchange_code="NSE",
                                         product_type="cash")
            if not (r or {}).get("Success"):
                raise RuntimeError(str((r or {}).get("Error"))[:80])
            self.breeze, self.breeze_note = b, "Breeze live"
            return True
        except Exception as e:
            self.breeze, self.breeze_note = None, f"Breeze not working ({str(e)[:60]})"
            return False

    def mode(self):
        if self.breeze:
            return "Breeze live prices"
        return "NSE live prices (no Breeze)" if self.nse_ok is not False else "stored data only (live prices unavailable)"

    def _breeze_bars(self, sym):
        code = (self.uni.get(sym) or {}).get("breeze")
        d = now().date()
        r = self.breeze.get_historical_data_v2(interval="1minute", from_date=f"{d}T09:15:00.000Z", to_date=f"{d}T15:30:00.000Z",
                                               stock_code=code, exchange_code="NSE", product_type="cash")
        rows = (r or {}).get("Success") or []
        return [(datetime.strptime(x["datetime"], "%Y-%m-%d %H:%M:%S"), float(x["high"]), float(x["low"]), float(x["close"]),
                 float(x.get("volume") or 0)) for x in rows]

    # -------------------------------------------------------------- snapshot
    def snapshot(self, sym):
        hit = self.cache.get(sym)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        lv = self.levels.get(sym, {})
        snap = dict(sym=sym, pc=lv.get("pc"), atr=lv.get("atr"), ph=lv.get("ph"), pl=lv.get("pl"), src="stored")
        bars = None
        if self.breeze:
            try:
                bars = self._breeze_bars(sym)
                snap["src"] = "Breeze"
            except Exception as e:
                log(f"Breeze bars failed for {sym}: {str(e)[:60]}")
        if not bars:
            try:
                pts, close = self.nse.chart(sym)
                bars = [(t, p, p, p, 0.0) for t, p in pts]
                snap["src"] = "NSE" if bars else snap["src"]
                snap["pc"] = snap["pc"] or close or None
                self.nse_ok = True
            except Exception as e:
                self.nse_ok = False
                log(f"NSE chart failed for {sym}: {str(e)[:60]}")
        if bars:
            snap["spot"] = bars[-1][3]
            snap["day_high"] = max(b[1] for b in bars)
            snap["day_low"] = min(b[2] for b in bars)
            first = [b for b in bars if dtime(9, 15) <= b[0].time() < dtime(9, 30)]
            if first and bars[-1][0].time() >= dtime(9, 30):
                snap["or_high"], snap["or_low"] = max(b[1] for b in first), min(b[2] for b in first)
                snap["or_open"], snap["or_close"] = first[0][3], first[-1][3]
            if snap.get("pc"):
                snap["chg"] = snap["spot"] / snap["pc"] - 1
        self.cache[sym] = (time.monotonic(), snap)
        return snap

    # -------------------------------------------------------------- option choice
    def pick_option(self, sym, right, budget):
        lot = (self.uni.get(sym) or {}).get("lot") or 0
        try:
            today = now().date()
            exps = [e for e in self.nse.expiries(sym) if (e - today).days >= 7]
            if not exps:
                return dict(note="no expiry with 7+ days left")
            exp = exps[0]
            spot, rows = self.nse.chain(sym, exp)
        except Exception as e:
            return dict(note=f"option chain unavailable ({str(e)[:50]})")
        if not rows or not spot or not lot:
            return dict(note="option chain empty")
        days = (exp - now().date()).days + 0.5
        cands = []
        for r in rows:
            leg = r[right]
            px = leg["ask"] or leg["ltp"]
            if px <= 0:
                continue
            dl = abs(bs_delta(spot, r["strike"], days, leg["iv"] or 30, right))
            itm = (r["strike"] <= spot) if right == "CE" else (r["strike"] >= spot)
            cands.append(dict(strike=r["strike"], ask=leg["ask"], bid=leg["bid"], ltp=leg["ltp"], px=px, delta=dl, itm=itm,
                              cost=px * lot, spread=(leg["ask"] - leg["bid"]) / leg["ask"] if leg["ask"] and leg["bid"] else None,
                              vol=leg["vol"]))
        atm = min(cands, key=lambda c: abs(c["strike"] - spot)) if cands else None
        fits = [c for c in cands if c["cost"] <= budget and (c["itm"] or c is atm) and c["delta"] <= 0.78]
        best = max(fits, key=lambda c: c["delta"]) if fits else None
        out = dict(expiry=exp, lot=lot, spot=spot, atm=atm)
        if best:
            out.update(best)
        else:
            out["note"] = f"no in-the-money or ATM strike within Rs {budget:,.0f}" + (f" (ATM costs Rs {atm['cost']:,.0f})" if atm else "")
            otm = [c for c in cands if c["cost"] <= budget and not c["itm"] and c["delta"] >= 0.25]
            if otm:
                out["otm"] = max(otm, key=lambda c: c["delta"])
        return out
