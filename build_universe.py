"""Build fno_universe.json from the research data on the Mac (run once, and after NSE lot changes).

Usage: ../.venv/bin/python -I build_universe.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "research"))
import v2_tgmatch as TM  # noqa: E402

uni = json.load(open(os.path.join(ROOT, "fno", "stock_fno", "universe.json")))
out = {}
for s, v in uni.items():
    if "NIFTY" in s:   # index-type contracts are not stocks
        continue
    al = sorted(set(TM.aliases_for(s, v.get("name"))))
    out[s] = {"name": v.get("name") or s, "lot": v.get("lot"), "breeze": v.get("breeze"), "aliases": al}
json.dump(out, open(os.path.join(HERE, "fno_universe.json"), "w"), indent=0)
print(f"{len(out)} F&O stocks written; e.g. TATAPOWER aliases {out.get('TATAPOWER', {}).get('aliases')}")
