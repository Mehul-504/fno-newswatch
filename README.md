# F&O news watch

Reads your Telegram news channels (read-only) and NSE company filings, matches posts to the 218 F&O stocks, has an AI
read each item (good/bad, how strong, event type, one-line headline) and sends a Telegram alert with the price picture
and a trade trigger. Never places orders.

**Prices, best source first**
1. Breeze, if today's session works (send `/session <id>` to the bot after your Breeze login): exact 15-minute range.
2. NSE website, no login: live price, minute prices for the 15-minute range, option chain with bid/ask.
3. Stored data only: previous close and normal daily range (refreshed every morning from NSE's daily files).

**Trade trigger** (the part that tested well): after 9:30, buy CE (PE) only if the first 15-minute candle closed
green (red) and price breaks its high (low); stop at the other side; exit by 15:15. Strike: the deepest in-the-money
strike whose lot costs at most ₹20,000, with its live ask and spread. News direction on its own did not test
profitable — treat the news as "which stock, which side to watch", not as the entry.

**Bot commands** (your chat only): `/session <id>`, `/status`, `/digest`, `/help`.
Digests at 08:50, 15:40 and 21:00. Every graded item is logged to `state/alerts/<date>.jsonl` for later scoring.

## Run on the Mac
    ../.venv/bin/python watch.py run --until 15:40          # live alerts
    ../.venv/bin/python watch.py once --dry --backfill 90   # test: last 90 minutes, print only

## Free cloud (GitHub Actions, runs with the Mac off)
1. `../.venv/bin/python make_tg_session.py` — a separate Telegram login for the cloud (type your number and code).
2. `../.venv/bin/python setup_cloud.py` — creates the public repo, stores the secrets, starts the first run.
Weekdays 07:30–23:00 IST in three back-to-back runs. Settings: `config.json` (channels, budget, digest times).
