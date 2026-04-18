"""
core/auth.py  —  Simple allowlist guard for personal bots.

Set ALLOWED_USER_IDS in .env as comma-separated Telegram user IDs.
Leave blank to allow everyone (not recommended for personal bots).
"""

import os
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes

_raw = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = {int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()}


def allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True  # open access if not configured
    return update.effective_user.id in ALLOWED_IDS


def require_auth(func):
    """Decorator for handler functions."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not allowed(update):
            await update.message.reply_text("⛔ Unauthorised.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
