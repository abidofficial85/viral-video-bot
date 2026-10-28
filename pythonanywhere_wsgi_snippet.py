# PythonAnywhere WSGI file — paste this into the web app's WSGI file
# (Web tab -> Code -> WSGI configuration file), replacing the placeholders.
# This file itself is NEVER committed with real values: it lives only in
# your private PythonAnywhere account. The bot token must never be sent in
# chat or put in git — paste it here yourself, in PythonAnywhere's editor.
#
# Steps:
#   1. PythonAnywhere dashboard -> Web -> Add a new web app -> Manual
#      configuration -> Python 3.10 (or newer).
#   2. In a Bash console:  git clone https://github.com/abidofficial85/viral-video-bot
#      (then the code lives at /home/USERNAME/viral-video-bot).
#   3. Open the WSGI configuration file, replace its whole content with
#      this file's content (with your own USERNAME, token and admin id),
#      save, then press the green Reload button.
#   4. Back in the bot code's directory the webhook registers automatically
#      on import (every Reload re-registers it); if WEBAPP_URL was empty at
#      import, POST to https://USERNAME.pythonanywhere.com/api/setup-webhook
#      after setting it.
#   5. In Telegram: /start the bot, and set the Menu Button to
#      https://USERNAME.pythonanywhere.com (via @BotFather -> Bot Settings
#      -> Menu Button).

import os
import sys

sys.path.insert(0, "/home/USERNAME/viral-video-bot")

os.environ["BOT_TOKEN"] = "PASTE_FRESH_BOT_TOKEN_HERE"
os.environ["ADMIN_ID"] = "PASTE_ADMIN_ID_HERE"
os.environ["BOT_MODE"] = "webhook"
os.environ["TG_PROXY"] = "http://proxy.server:3128"
os.environ["WEBAPP_URL"] = "https://USERNAME.pythonanywhere.com"
os.environ["DATA_DIR"] = "/home/USERNAME/viral-video-bot"

from main import application  # noqa: E402,F401  (WSGI callable)
