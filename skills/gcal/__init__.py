"""
skills/gcal/__init__.py  —  Google Calendar Event Creator skill.

Flow:
  1. User sends a message like "meeting with Kate tomorrow at 3pm for 1hr"
  2. Claude extracts structured fields (title, date, time, duration, etc.)
  3. Bot replies with a preview: "Create this event? [Confirm] [Cancel]"
  4. User confirms → event is created via Google Calendar API

OAuth setup (one-time):
  1. Create a project in Google Cloud Console
  2. Enable Google Calendar API
  3. Create OAuth 2.0 credentials (Desktop app type)
  4. Download as client_secret.json → place in project root
  5. Run:  python -m skills.gcal.auth_setup
     This opens a browser, you authorise, and google_token.json is saved.
  6. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env
"""

from __future__ import annotations
import os
import json
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler

from core.skill_base import BaseSkill, SkillResult, registry

SCOPES        = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE    = Path("google_token.pickle")
SECRETS_FILE  = Path("client_secret.json")
TIMEZONE      = "Europe/London"           # Jorge's timezone

_claude = anthropic.Anthropic()

# Pending confirmations: {callback_key: event_body}
_pending: dict[str, dict] = {}


class GCalSkill(BaseSkill):
    name        = "gcal"
    description = "Create Google Calendar events from natural language"
    commands    = ["/cal", "/event", "/gcal"]
    examples    = [
        "meeting with Kate tomorrow at 3pm for 1 hour",
        "dentist appointment Friday 10am",
        "lunch with Jess next Tuesday 12:30",
        "/cal team sync Thursday 2pm 45min",
    ]

    def __init__(self) -> None:
        self._service = None

    async def on_load(self) -> None:
        try:
            self._service = _get_calendar_service()
            print("[gcal] Google Calendar connected ✓")
        except Exception as e:
            print(f"[gcal] WARNING: Could not connect to Google Calendar — {e}")
            print("[gcal] Run:  python -m skills.gcal.auth_setup  to authorise")

    # ── main entry ────────────────────────────────────────────────────────────

    async def handle(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
        extracted: dict | None = None,
    ) -> SkillResult:
        if not self._service:
            return SkillResult(
                "⚠️ Google Calendar not connected.\n"
                "Run `python -m skills.gcal.auth_setup` on your server to authorise.",
                success=False,
            )

        ext = extracted or await _extract_event(user_text)

        if not ext.get("title"):
            return SkillResult(
                "I couldn't find an event title. Try:\n"
                "_meeting with Kate tomorrow at 3pm_",
                success=False,
            )

        # Build a preview and store pending confirmation
        preview, event_body = _build_preview(ext)
        key = f"{update.effective_user.id}:{datetime.now().timestamp()}"
        _pending[key] = event_body

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Create event", callback_data=f"gcal_confirm:{key}"),
            InlineKeyboardButton("❌ Cancel",        callback_data=f"gcal_cancel:{key}"),
        ]])

        await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)
        return SkillResult("", success=True)   # reply already sent above

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        action, key = query.data.split(":", 1)

        if action == "gcal_cancel":
            _pending.pop(key, None)
            await query.edit_message_text("❌ Cancelled.")
            return

        event_body = _pending.pop(key, None)
        if not event_body:
            await query.edit_message_text("⚠️ Event expired. Please try again.")
            return

        try:
            created = self._service.events().insert(
                calendarId="primary", body=event_body
            ).execute()
            link = created.get("htmlLink", "")
            await query.edit_message_text(
                f"✅ *Event created!*\n\n"
                f"[Open in Google Calendar]({link})",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to create event: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _extract_event(user_text: str) -> dict:
    """Ask Claude to extract structured event details from free text."""
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y")
    response = _claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=f"""Today is {today}. Extract calendar event details from the user message.
Respond ONLY with JSON, no markdown:
{{
  "title": string,
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "duration_minutes": int (default 60),
  "description": string or null,
  "location": string or null
}}
If date/time is ambiguous, make a reasonable assumption and include it.""",
        messages=[{"role": "user", "content": user_text}],
    )
    raw = response.content[0].text.strip().strip("```json").strip("```")
    return json.loads(raw)


def _build_preview(ext: dict) -> tuple[str, dict]:
    """Build a Markdown preview string and a Google Calendar event body."""
    tz       = ZoneInfo(TIMEZONE)
    date_str = ext.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
    time_str = ext.get("start_time", "09:00")
    duration = int(ext.get("duration_minutes", 60))

    start_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00").replace(tzinfo=tz)
    end_dt   = start_dt + timedelta(minutes=duration)

    fmt_date = start_dt.strftime("%A %d %B")
    fmt_time = start_dt.strftime("%H:%M")
    fmt_end  = end_dt.strftime("%H:%M")
    hrs      = duration // 60
    mins     = duration % 60
    dur_str  = (f"{hrs}h " if hrs else "") + (f"{mins}m" if mins else "")

    lines = [
        f"📅 *{ext['title']}*",
        f"🗓  {fmt_date}",
        f"⏰  {fmt_time} – {fmt_end}  ({dur_str.strip()})",
    ]
    if ext.get("location"):
        lines.append(f"📍  {ext['location']}")
    if ext.get("description"):
        lines.append(f"📝  {ext['description']}")

    preview = "\n".join(lines) + "\n\nCreate this event?"

    event_body = {
        "summary": ext["title"],
        "start":   {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end":     {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
    }
    if ext.get("description"):
        event_body["description"] = ext["description"]
    if ext.get("location"):
        event_body["location"] = ext["location"]

    return preview, event_body


def _get_calendar_service():
    """
    Loads credentials from one of two sources (in priority order):
      1. GOOGLE_TOKEN_B64 env var  — used on Railway (base64-encoded pickle)
      2. google_token.pickle file  — used locally

    To refresh: re-run auth_setup locally, then re-encode and update
    the Railway env var with:  python scripts/encode_token.py
    """
    import base64
    creds = None

    b64 = os.getenv("GOOGLE_TOKEN_B64")
    if b64:
        creds = pickle.loads(base64.b64decode(b64))
    elif TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds:
        raise RuntimeError(
            "No Google token found. "
            "Run auth_setup locally then set GOOGLE_TOKEN_B64 in Railway."
        )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Persist refreshed token back to env (in-memory only; update Railway var manually)
        print("[gcal] Token refreshed — remember to update GOOGLE_TOKEN_B64 in Railway")

    if not creds.valid:
        raise RuntimeError("Google token is invalid — re-run auth_setup")

    return build("calendar", "v3", credentials=creds)


# Register skill instance (callback handler wired in bot.py)
_skill_instance = GCalSkill()
registry.register(_skill_instance)
