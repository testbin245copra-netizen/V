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

# ─────────────────────────────────────────────────────────
#  Price range for product selection
# ─────────────────────────────────────────────────────────
MIN_PRODUCT_PRICE = float(os.environ.get("MIN_PRODUCT_PRICE", "0.01"))
MAX_PRODUCT_PRICE = float(os.environ.get("MAX_PRODUCT_PRICE", "30.0"))
MAX_SITE_AMOUNT   = float(os.environ.get("MAX_SITE_AMOUNT",   "30.0"))
