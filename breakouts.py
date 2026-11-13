"""Chart breakouts for every NSE stock: falling trendlines, flat tops, rising trendlines (breakdowns).

Daily prices come from NSE's public end-of-day files (sec_bhavdata_full), kept for ~300 trading days in state/bhav/
and adjusted for splits/bonuses (a >20% open gap vs the previous close is treated as a corporate action).

Lines, drawn the way a trader does on a daily chart:
  falling resistance  two or more swing highs (5 bars each side), the earlier one higher, at least 30 bars apart in
                      total, no close above the line in between; the line with most touches (then longest) wins
  rising support      the mirror image with swing lows (for breakdowns / PE)
  flat top / bottom   two or more swing highs (lows) within 1.5% of each other, no close beyond since
Breakout: close (or live price) beyond the line by 0.5%+, after being on the other side the previous day.
Checks: volume vs 20-day average, close location in the day's range (long upper wick = weak), EMA 9/20/50/200,
RSI(14), room to the next swing level, how far past the line (in ATRs), line length and touches.
"""

import csv
import gzip
import io
import os
import urllib.request
from datetime import date, timedelta

import numpy as np

from common import STATE, log, now

BHAV = os.path.join(STATE, "bhav")
K = 5            # swing point: highest/lowest of 5 bars each side
TOL = 0.005      # 0.5% beyond the line
COLS = ("PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY", "TURNOVER_LACS")


# ------------------------------------------------------------------ daily store

def _fetch(d):
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
    try:
        raw = urllib.request.urlopen(req, timeout=40).read().decode()
    except Exception:
        return None
    out = io.StringIO()
    w = csv.writer(out)
    for r in csv.DictReader(io.StringIO(raw)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") == "EQ":
            w.writerow([r["SYMBOL"]] + [r.get(c, "") for c in COLS])
    return out.getvalue()


def update_store(days=300):
    """Download missing trading days (newest first) until `days` are on disk; returns the number added."""
    os.makedirs(BHAV, exist_ok=True)
    have = {f[:8] for f in os.listdir(BHAV)}
    added, d, misses = 0, now().date(), 0
    while len(have) < days and misses < 40:
        if d.weekday() < 5 and f"{d:%Y%m%d}" not in have:
            txt = _fetch(d)
            if txt:
                with gzip.open(os.path.join(BHAV, f"{d:%Y%m%d}.csv.gz"), "wt") as f:
                    f.write(txt)
                have.add(f"{d:%Y%m%d}")
                added += 1
                misses = 0
            elif (now().date() - d).days > 3:
                misses += 1
        d -= timedelta(days=1)
        if (now().date() - d).days > 500:
            break
    for f in sorted(os.listdir(BHAV))[:-days - 20]:
        os.remove(os.path.join(BHAV, f))
    return added


def load_store():
    """{sym: dict(days, o, h, l, c, v, turnover)} oldest first, split/bonus adjusted."""
    files = sorted(f for f in os.listdir(BHAV) if f.endswith(".csv.gz")) if os.path.isdir(BHAV) else []
    rows = {}
    for f in files:
        d = date(int(f[:4]), int(f[4:6]), int(f[6:8]))
        with gzip.open(os.path.join(BHAV, f), "rt") as fh:
            for r in csv.reader(fh):
                try:
                    vals = [float(x) for x in r[1:8]]
                except ValueError:
                    continue
                rows.setdefault(r[0], []).append((d, *vals))
    out = {}
    for s, rs in rows.items():
        if len(rs) < 60:
            continue
        a = np.array([x[1:] for x in rs], float)   # pc, o, h, l, c, v, turnover
        pc, o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
        f = np.ones(len(rs))
        for i in range(1, len(rs)):
            if pc[i] > 0 and abs(o[i] / pc[i] - 1) > 0.2:      # corporate action: scale everything before it
                f[:i] *= o[i] / pc[i]
        out[s] = dict(days=[x[0] for x in rs], o=o * f, h=h * f, l=l * f, c=c * f, v=a[:, 5] / f, t=a[:, 6])
    return out


# ------------------------------------------------------------------ indicators

def ema(x, n):
    out, k = np.empty_like(x), 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = np.zeros_like(c), np.zeros_like(c)
    au[n], ad[n] = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n + 1, len(c)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 - 100 / (1 + au / ad)


def atr(h, l, c, n=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return np.convolve(tr, np.ones(n) / n, mode="full")[:len(tr)]


def pivots(x, high=True):
    idx = []
    for i in range(K, len(x) - K):
        w = x[i - K:i + K + 1]
        if (x[i] == w.max()) if high else (x[i] == w.min()):
            idx.append(i)
    return idx


# ------------------------------------------------------------------ lines

def best_line(h, l, c, end, up_side=True, lookback=250):
    """Best valid sloping line using bars < end (end = index of the bar being judged). Returns dict or None.
    up_side=True: falling resistance through swing highs; False: rising support through swing lows."""
    x = h if up_side else l
    lo = max(0, end - lookback)
    piv = [p for p in pivots(x[:end]) if p >= lo] if up_side else [p for p in pivots(x[:end], high=False) if p >= lo]
    if len(piv) < 2:
        return None
    best = None
    for j in range(len(piv) - 1, max(len(piv) - 7, 0), -1):        # later point: one of the last 6 swings
        p2 = piv[j]
        for p1 in piv[:j]:
            if p2 - p1 < 10 or end - p1 < 30:
                continue
            if up_side and not x[p1] > x[p2]:
                continue
            if not up_side and not x[p1] < x[p2]:
                continue
            slope = (x[p2] - x[p1]) / (p2 - p1)
            idx = np.arange(p1, end)
            line = x[p1] + slope * (idx - p1)
            if up_side and np.any(c[p1:end] > line * (1 + TOL)):
                continue
            if not up_side and np.any(c[p1:end] < line * (1 - TOL)):
                continue
            touches = sum(1 for p in piv if p1 <= p < end and abs(x[p] / (x[p1] + slope * (p - p1)) - 1) <= 0.015)
            score = touches * 1000 + (end - p1)
            if best is None or score > best["score"]:
                best = dict(p1=p1, p2=p2, slope=slope, base=x[p1], touches=touches, span=end - p1, score=score)
    return best


def line_at(ln, i):
    return ln["base"] + ln["slope"] * (i - ln["p1"])


def flat_level(h, l, c, end, up_side=True, lookback=120):
    x = h if up_side else l
    piv = [p for p in pivots(x[:end], high=up_side) if p >= end - lookback]
    best = None
    for p in piv:
        grp = [q for q in piv if abs(x[q] / x[p] - 1) <= 0.015]
        if len(grp) < 2:
            continue
        lvl = max(x[q] for q in grp) if up_side else min(x[q] for q in grp)
        first = min(grp)
        if up_side and np.any(c[first:end] > lvl * (1 + TOL)):
            continue
        if not up_side and np.any(c[first:end] < lvl * (1 - TOL)):
            continue
        if best is None or len(grp) > best["touches"]:
            best = dict(level=lvl, touches=len(grp), span=end - first)
    return best


# ------------------------------------------------------------------ checks and scan

def checks(s, i, side, level):
    h, l, c, v = s["h"], s["l"], s["c"], s["v"]
    e = {n: ema(c[:i + 1], n)[-1] for n in (9, 20, 50, 200)} if i >= 60 else {}
    r = rsi(c[:i + 1])[-1] if i > 20 else np.nan
    a = atr(h[:i + 1], l[:i + 1], c[:i + 1])[-1]
    vr = v[i] / v[max(0, i - 20):i].mean() if i > 5 and v[max(0, i - 20):i].mean() > 0 else np.nan
    rng = h[i] - l[i]
    clv = (c[i] - l[i]) / rng if rng > 0 else 0.5
    up = side == "up"
    piv = pivots(h[:i]) if up else pivots(l[:i], high=False)
    nxt = [h[p] for p in piv if h[p] > c[i] * 1.005] if up else [l[p] for p in piv if l[p] < c[i] * 0.995]
    room = ((min(nxt) / c[i] - 1) if up else (1 - max(nxt) / c[i])) if nxt else None
    ext = abs(c[i] - level) / a if a else np.nan
    ok = {
        "volume": (vr >= 1.5, f"volume {vr:.1f}× normal" if vr == vr else "volume n/a"),
        "candle": ((clv >= 0.6) if up else (clv <= 0.4), "strong close" if (clv >= 0.6 if up else clv <= 0.4) else "long wick (weak close)"),
        "trend": ((c[i] > e.get(20, 0) and c[i] > e.get(50, 0)) if up else (c[i] < e.get(20, 1e9) and c[i] < e.get(50, 1e9)),
                  ("above" if up else "below") + " 20 & 50 EMA" if e else "EMAs n/a"),
        "ema200": ((c[i] > e.get(200, 0)) if up else (c[i] < e.get(200, 1e9)), ("above" if c[i] > e.get(200, 0) else "below") + " 200 EMA" if e else "200 EMA n/a"),
        "rsi": ((r < 75) if up else (r > 25), f"RSI {r:.0f}" + (" stretched" if (r >= 75 if up else r <= 25) else "")),
        "room": (room is None or room >= 0.02, "blue sky (no resistance above)" if room is None and up else
                 "no support below" if room is None else f"room to next level {room * 100:.1f}%"),
        "not late": (ext <= 1, f"{ext:.1f} ATR past the line"),
    }
    return ok, dict(vol_ratio=vr, clv=clv, rsi=r, room=room, ext_atr=ext, atr=a, ema=e)


def scan(store, fno, i_from_end=1, near_pct=0.03):
    """Breakouts on the latest day in the store, plus F&O stocks within near_pct of a line (tomorrow's watchlist)."""
    events, watch = [], {}
    latest = max(x["days"][-1] for x in store.values())
    fund = ("ETF", "BEES", "NIFTY", "SENSEX", "GOLD", "SILVER", "LIQUID", "MON100", "MOM", "INAV", "BETA")
    for sym, s in store.items():
        n = len(s["c"])
        i = n - i_from_end
        if i < 70 or (i_from_end == 1 and s["days"][i] != latest):
            continue
        if sym not in fno and any(f in sym for f in fund):
            continue   # ETFs and index funds are not stocks
        c = s["c"]
        for up in (True, False):
            ln = best_line(s["h"], s["l"], c, i, up_side=up)
            kinds = []
            if ln and ln["span"] >= 30 and ln["touches"] >= 2:
                kinds.append(("trendline", line_at(ln, i), line_at(ln, i - 1), ln, line_at(ln, i + 1)))
            fl = flat_level(s["h"], s["l"], c, i, up_side=up)
            if fl and fl["span"] >= 20:
                kinds.append(("flat", fl["level"], fl["level"], fl, fl["level"]))
            for kind, L, Lprev, meta, Lnext in kinds:
                crossed = c[i] > L * (1 + TOL) and c[i - 1] <= Lprev * (1 + TOL) if up else \
                    c[i] < L * (1 - TOL) and c[i - 1] >= Lprev * (1 - TOL)
                side = "up" if up else "down"
                if crossed:
                    ok, info = checks(s, i, side, L)
                    events.append(dict(sym=sym, fno=sym in fno, side=side, kind=kind, level=L, close=c[i],
                                       touches=meta["touches"], span=meta["span"], turnover_cr=s["t"][i] / 100,
                                       score=sum(1 for v in ok.values() if v[0]), checks={k: v[1] for k, v in ok.items()},
                                       passed=[k for k, v in ok.items() if v[0]], info=info, day=str(s["days"][i])))
                elif sym in fno:
                    gap = (Lnext / c[i] - 1) if up else (1 - Lnext / c[i])
                    if 0 < gap <= near_pct:
                        cur = watch.get(sym)
                        if cur is None or gap < cur["gap"]:
                            watch[sym] = dict(side=side, kind=kind, level=round(Lnext, 2), gap=gap,
                                              touches=meta["touches"], span=meta["span"])
    best = {}
    for e in events:   # one line per stock and side: the one with the best score, then most touches
        k = (e["sym"], e["side"])
        if k not in best or (e["score"], e["touches"]) > (best[k]["score"], best[k]["touches"]):
            best[k] = e
    return list(best.values()), watch


def live_checks(s, side, px, level, line_meta):
    """Checks for a breakout happening now: yesterday's trend/RSI, room from the live price, distance past the line."""
    c, h, l = s["c"], s["h"], s["l"]
    i = len(c) - 1
    up = side == "up"
    e = {n: ema(c, n)[-1] for n in (20, 50, 200)} if len(c) >= 60 else {}
    r = rsi(c)[-1] if len(c) > 20 else float("nan")
    a = atr(h, l, c)[-1]
    piv = pivots(h) if up else pivots(l, high=False)
    nxt = [h[p] for p in piv if h[p] > px * 1.005] if up else [l[p] for p in piv if l[p] < px * 0.995]
    room = ((min(nxt) / px - 1) if up else (1 - max(nxt) / px)) if nxt else None
    ext = abs(px - level) / a if a else float("nan")
    out = [
        (((px > e.get(20, 0) and px > e.get(50, 0)) if up else (px < e.get(20, 1e9) and px < e.get(50, 1e9))),
         ("above" if up else "below") + " 20 & 50 EMA"),
        (((px > e.get(200, 0)) if up else (px < e.get(200, 1e9))), ("above" if px > e.get(200, 0) else "below") + " 200 EMA"),
        (((r < 75) if up else (r > 25)), f"RSI {r:.0f}" + (" stretched" if (r >= 75 if up else r <= 25) else "")),
        ((room is None or room >= 0.02), "blue sky (no resistance above)" if room is None and up else
         "no support below" if room is None else f"room to next level {room * 100:.1f}%"),
        ((ext <= 1), f"{ext:.1f} ATR past the line"),
    ]
    return out, sum(1 for ok, _ in out if ok)
