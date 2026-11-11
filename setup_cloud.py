"""One-time cloud setup: create the GitHub repo, upload the code, store the secrets, start the first run.

Needs: GitHub CLI logged in (gh), and state/tg_session.txt from make_tg_session.py.
Secrets are read from the Mac's credential files and passed to `gh secret set` on stdin: they are never printed, never
written into the repo. The repo is public (free unlimited Actions minutes); it contains only code and the channel list.

Usage: ../.venv/bin/python setup_cloud.py [owner/repo]     (default Mehul-504/fno-newswatch)
"""

import os
import secrets as pysecrets
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import STATE, secret  # noqa: E402

REPO = sys.argv[1] if len(sys.argv) > 1 else "Mehul-504/fno-newswatch"
NAMES = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "LLM_URL", "LLM_KEY",
         "LLM_MODEL", "LLM_BACKUP_MODEL", "BREEZE_API_KEY", "BREEZE_API_SECRET"]


def run(cmd, **kw):
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=True, cwd=HERE, **kw)


def main():
    tg = os.path.join(STATE, "tg_session.txt")
    if not os.path.exists(tg):
        sys.exit("Run make_tg_session.py first (it creates the separate cloud Telegram login).")
    if input(f"Create public repo {REPO}, upload the code and store {len(NAMES) + 2} secrets? Type yes: ").strip() != "yes":
        sys.exit("Stopped, nothing changed.")
    if not os.path.isdir(os.path.join(HERE, ".git")):
        run(["git", "init", "-q", "-b", "main"])
    run(["git", "add", "-A"])
    subprocess.run(["git", "commit", "-q", "-m", "F&O news watch: Telegram + NSE filings -> AI read -> Telegram alerts"], cwd=HERE)
    exists = subprocess.run(["gh", "repo", "view", REPO], cwd=HERE, capture_output=True).returncode == 0
    if not exists:
        run(["gh", "repo", "create", REPO, "--public", "--source", ".", "--push",
             "--description", "F&O stock news alerts on Telegram (read-only news watcher)"])
    else:
        run(["git", "push", "-u", "origin", "main"])
    values = {n: secret(n) for n in NAMES}
    values["TG_SESSION"] = open(tg).read().strip()
    values["STATE_KEY"] = pysecrets.token_hex(32)
    for n, v in values.items():
        if not v:
            print(f"  skip {n} (no local value)")
            continue
        subprocess.run(["gh", "secret", "set", n, "-R", REPO], input=str(v).encode(), check=True, cwd=HERE)
        print(f"  secret {n} stored")
    run(["gh", "workflow", "run", "newswatch", "-R", REPO, "-f", "until=23:00"])
    print(f"\nDone. Runs: https://github.com/{REPO}/actions  - you will get a 'News watch on' message on Telegram.")


if __name__ == "__main__":
    main()
