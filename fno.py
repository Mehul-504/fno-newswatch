"""F&O stock list and matching posts to stocks.

fno_universe.json: {SYM: {"name", "lot", "breeze", "aliases": [normalised strings]}} built by build_universe.py.
Roundup posts ("stocks in news" lists) are split line by line so each stock gets only its own line as context.
"""

import json
import os
import re

from common import HERE


def norm(s):
    return " " + re.sub(r"[^A-Z0-9&]+", " ", (s or "").upper()) + " "


def load():
    return json.load(open(os.path.join(HERE, "fno_universe.json")))


def match(text, uni):
    """[(sym, context text)] for the F&O stocks a post talks about."""
    whole = norm(text)
    hits = [s for s, v in uni.items() if any(a in whole for a in v["aliases"])]
    if not hits:
        return []
    if len(hits) <= 2:
        return [(s, text) for s in hits]
    out = {}
    for line in re.split(r"[\n•▪➡️🔹🔸|]+", text):
        nl = norm(line)
        on_line = [s for s in hits if any(a in nl for a in uni[s]["aliases"])]
        if 1 <= len(on_line) <= 2 and len(line.strip()) > 12:
            for s in on_line:
                out.setdefault(s, line.strip())
    if not out and len(hits) <= 4:
        return [(s, text) for s in hits]
    return list(out.items())[:12]
