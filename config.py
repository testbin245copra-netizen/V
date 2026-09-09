# ─────────────────────────────────────────────────────────
#  Shopify Checker API — config
#  All values can be overridden via environment variables
# ─────────────────────────────────────────────────────────

import os

# Port to listen on
PORT = int(os.environ.get("CHECKER_PORT", os.environ.get("PORT", "8002")))

# API key for auth — leave empty (or unset) to disable auth
CHECKER_API_KEY = os.environ.get("CHECKER_API_KEY", "")

# Site list files (used only if you want the bot to pick a random site)
SITE_FILE     = os.environ.get("SITE_FILE",     "site.txt")
SITE_LOW_FILE = os.environ.get("SITE_LOW_FILE", "site_low.txt")
SITE_MID_FILE = os.environ.get("SITE_MID_FILE", "site_mid.txt")
