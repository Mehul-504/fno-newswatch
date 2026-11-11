"""Log in to Telegram once for the cloud watcher and save the login as a string (run this yourself, in a terminal).

It creates a SEPARATE login ("NewsWatch Cloud") so the cloud never shares the Mac's session; sharing one session from
two places at the same time can make Telegram log both out. You type your phone number and the code Telegram sends;
nothing is posted, joined or sent. The string is saved to state/tg_session.txt (only you can read it) for
setup_cloud.sh to store as the TG_SESSION secret. Anyone with that string can read your Telegram, so never share it.

Usage: ../.venv/bin/python make_tg_session.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import STATE, secret  # noqa: E402

from telethon.sessions import StringSession  # noqa: E402
from telethon.sync import TelegramClient  # noqa: E402

path = os.path.join(STATE, "tg_session.txt")
os.makedirs(STATE, exist_ok=True)
with TelegramClient(StringSession(), int(secret("TELEGRAM_API_ID")), secret("TELEGRAM_API_HASH"),
                    device_model="NewsWatch Cloud", app_version="1.0") as client:
    me = client.get_me()
    s = client.session.save()
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(s)
print(f"Logged in as {me.first_name}. Cloud login saved to {path} (private file, do not share).")
