"""AI reading of news: per stock, good/bad, how strong, what kind of event, a one-line English headline.

The model sees only public news text (never prices). If the gateway is down, a keyword fallback gives a low-confidence
read so alerts still go out.
"""

import re

from common import first_json, llm_chat, log

RULES = """You read Indian stock-market news for a trader. For EACH company listed, judge ONLY from its news text.
You do not know the share price and must not guess it.

Return a JSON array with one object per company, keys exactly:
  "sym": the NSE symbol given
  "direction": 1 if the news is good for this company's share price, -1 if bad, 0 if mixed, unclear, routine or not
               really about this company
  "strength": "strong", "weak" or "none"
  "event_type": short label: results, order, regulatory, broker, management, block deal, bulk deal, stake sale,
                corporate, sector, macro, tip/call, other
  "surprise": "beat", "miss", "inline" or "unknown" (results vs estimates)
  "confidence": "high", "medium" or "low"
  "headline": the news in plain English, at most 14 words (translate Hindi)

Rules:
- Results: strong when clearly better/worse than estimates (5%+ on profit or EBITDA) or a very large YoY profit change
  (+40% / -20%) without an opposite headline metric; weak when moderate or mixed; clearly mixed -> 0.
- Brokers: maintained Buy/Sell -> weak; big upgrades/downgrades or several brokers -> strong.
- Strong: regulator action (FDA, RBI, SEBI), takeover talk, CEO/MD exit, orders very large for the company,
  big promoter/block/bulk deals.
- A trading tip or call ("buy above 120, target 130") is event_type "tip/call", direction from the call, strength weak.
- Routine filings, meeting notices, price-only commentary, other companies' news -> direction 0.
Output only the JSON array."""

GOOD = ("BEAT", "ORDER", "BAGS", "WINS", "UPGRADE", "RECORD HIGH", "PROFIT UP", "PROFIT JUMPS", "SURGE", "APPROVAL",
        "ACQUIRE", "BUYBACK", "BONUS", "TARGET RAISED", "STRONG", "GROWTH")
BAD = ("MISS", "DOWNGRADE", "LOSS", "PROFIT DOWN", "FALLS", "PENALTY", "FINE", "RAID", "RESIGN", "FRAUD", "BAN",
       "WARNING LETTER", "OBSERVATIONS", "TARGET CUT", "WEAK", "DECLINE", "PLEDGE")


def fallback(sym, text):
    u = text.upper()
    g, b = sum(w in u for w in GOOD), sum(w in u for w in BAD)
    d = 1 if g > b else -1 if b > g else 0
    head = re.sub(r"\s+", " ", text).strip()[:90]
    return dict(sym=sym, direction=d, strength="weak" if d else "none", event_type="other", surprise="unknown",
                confidence="low", headline=head, by="keywords")


def grade(items, uni):
    """items: [(sym, context)] from one post -> {sym: grade}."""
    if not items:
        return {}
    lines = [f"- {s} ({uni[s]['name']}): {re.sub(r'\s+', ' ', c)[:600]}" for s, c in items]
    try:
        out = first_json(llm_chat([{"role": "system", "content": RULES},
                                   {"role": "user", "content": "Companies and their news:\n" + "\n".join(lines)}]))
        if isinstance(out, dict):
            out = [out]
        res = {}
        for o in out:
            s = str(o.get("sym", "")).upper()
            if s not in dict(items):
                continue
            d = int(o.get("direction", 0)) if str(o.get("direction", "0")).lstrip("-").isdigit() else 0
            res[s] = dict(sym=s, direction=d if d in (-1, 0, 1) else 0,
                          strength=o.get("strength") if o.get("strength") in ("strong", "weak") and d else "none",
                          event_type=str(o.get("event_type", "other"))[:30], surprise=str(o.get("surprise", "unknown"))[:10],
                          confidence=str(o.get("confidence", "low"))[:8], headline=str(o.get("headline", ""))[:140], by="ai")
        for s, c in items:
            res.setdefault(s, fallback(s, c))
        return res
    except Exception as e:
        log(f"AI read failed ({str(e)[:60]}); keyword fallback")
        return {s: fallback(s, c) for s, c in items}
