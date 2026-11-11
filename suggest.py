"""Alert text: the news, the AI read, the price picture and a trade trigger.

The trigger is the tested part: wait for the 15-minute opening range, buy CE (PE) only on a break of its high (low),
stop at the other side, exit by 15:15. News only tells which stock and which side to watch (news direction on its own
did not test profitable). Strike: deepest in-the-money within the budget.
"""

from datetime import time as dtime

from common import esc, now


def rs(x, d=1):
    return "-" if x is None else f"₹{x:,.{d}f}"


def plan_text(sym, side, snap, opt, budget):
    t = now().time()
    up = side == "CE"
    lines = []
    if t >= dtime(15, 0) or t < dtime(9, 15):
        when = "Tomorrow" if t >= dtime(15, 0) else "Today"
        lines.append(f"<b>{when}'s plan ({side}):</b> after 9:30, if the first 15-min candle closes "
                     f"{'green' if up else 'red'} and price breaks its {'high' if up else 'low'}, buy {side}; "
                     f"stop at its {'low' if up else 'high'}; exit by 15:15.")
    elif t >= dtime(9, 30) and not snap.get("or_high"):
        lines.append(f"<b>Watch {side}:</b> live 15-min range unavailable for this stock right now; buy only if it breaks "
                     f"today's {'high' if up else 'low'} with momentum, stop at the other side of the first 15 minutes, exit by 15:15.")
    elif t < dtime(9, 30):
        lines.append(f"<b>Plan ({side}):</b> 15-min range still forming. After 9:30 buy {side} only if the first candle is "
                     f"{'green' if up else 'red'} and price breaks its {'high' if up else 'low'}; stop at the other side; exit by 15:15.")
    else:
        hi, lo, spot = snap["or_high"], snap["or_low"], snap.get("spot")
        trig, stop = (hi, lo) if up else (lo, hi)
        candle_ok = (snap.get("or_close", 0) > snap.get("or_open", 0)) == up
        broken = spot is not None and ((spot > trig) if up else (spot < trig))
        far = spot is not None and abs(spot / trig - 1) > 0.01
        if broken and far:
            lines.append(f"<b>Watch {side}:</b> already {abs(spot / trig - 1) * 100:.1f}% past the 15-min "
                         f"{'high' if up else 'low'} {rs(trig)} — too late to chase; enter only if it comes back to {rs(trig)}.")
        elif broken:
            lines.append(f"<b>Trigger hit {side}:</b> price {rs(spot)} is past the 15-min {'high' if up else 'low'} {rs(trig)}.")
        else:
            lines.append(f"<b>Watch {side}:</b> buy if price {'rises above' if up else 'falls below'} {rs(trig)} "
                         f"(15-min {'high' if up else 'low'}) before 15:00.")
        lines.append(f"Stop {rs(stop)} (other side of the range) · exit by 15:15"
                     + ("" if candle_ok else " · ⚠️ first candle went the other way (rule says skip)"))
    if opt.get("strike"):
        sp = f" · spread {opt['spread'] * 100:.1f}%" if opt.get("spread") is not None else ""
        warn = " ⚠️ wide spread" if (opt.get("spread") or 0) > 0.03 else ""
        lines.append(f"Option: <b>{opt['strike']:g} {side}</b> {opt['expiry']:%d-%b} · ask {rs(opt['px'], 2)} × {opt['lot']:,} = "
                     f"<b>{rs(opt['cost'], 0)}</b> · delta {opt['delta']:.2f}{sp}{warn}")
    elif opt.get("note"):
        lines.append(f"Option: {esc(opt['note'])}")
        o = opt.get("otm")
        if o:
            lines.append(f"Nearest affordable: {o['strike']:g} {side} · ask {rs(o['px'], 2)} × {opt['lot']:,} = {rs(o['cost'], 0)} · "
                         f"delta {o['delta']:.2f} (out of the money — riskier, untested)")
    return lines


def alert(sym, g, post, others, snap, opt, budget, model_rank=None, mode=""):
    good = g["direction"] > 0
    icon = "🟢" if good else "🔴"
    tip = g.get("event_type") == "tip/call"
    head = (f"{icon} <b>{esc(sym)}</b> · {esc(g['event_type'])} · <b>{g['strength']} {'good' if good else 'bad'}</b>"
            f"{' (tip)' if tip else ''} · {post['ts']:%H:%M}")
    src = f"<i>{esc(post['source'])}</i>" + (f" (+{others} more)" if others else "")
    lines = [head, esc(g.get("headline") or post["text"][:120]), src]
    px = []
    if snap.get("spot") is not None:
        ch = f" ({snap['chg'] * 100:+.1f}% today)" if snap.get("chg") is not None else ""
        px.append(f"Price {rs(snap['spot'])}{ch}")
    elif snap.get("pc"):
        px.append(f"Prev close {rs(snap['pc'])}")
    if snap.get("or_high"):
        px.append(f"15-min range {rs(snap['or_low'])}–{rs(snap['or_high'])}")
    if snap.get("atr") and snap.get("pc"):
        px.append(f"normal day ±{snap['atr'] / snap['pc'] * 100:.1f}%")
    px.append(f"[{snap.get('src', 'stored')}]")
    lines.append(" · ".join(px))
    if model_rank is not None:
        lean = "up" if model_rank > 0.6 else "down" if model_rank < 0.4 else "neutral"
        agree = (lean == "up" and good) or (lean == "down" and not good)
        lines.append(f"Daily model leans {lean}" + (" ✅ agrees" if agree else " ❌ disagrees" if lean != "neutral" else ""))
    lines += plan_text(sym, "CE" if good else "PE", snap, opt, budget)
    if g.get("confidence") == "low" or g.get("by") == "keywords":
        lines.append("<i>Low-confidence read — check the news yourself.</i>")
    return "\n".join(lines)
