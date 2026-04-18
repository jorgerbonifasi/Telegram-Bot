"""
skills/gcal/__init__.py  —  Google Calendar Event Creator

Follows the gcal-event-creator SKILL.md spec exactly:
- Extracts event details from any input (text, described screenshots, etc.)
- Always shows a preview with emoji, reminders, and Claude note BEFORE creating
- Applies event-type defaults (duration, reminders, emoji)
- Handles natural language dates and London timezone
- Confirm/cancel via inline buttons
"""

from __future__ import annotations
import os
import json
import pickle
import base64
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.skill_base import BaseSkill, SkillResult, registry

SCOPES     = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = Path("google_token.pickle")
TIMEZONE   = "Europe/London"

_claude  = anthropic.Anthropic()
_pending: dict[str, dict] = {}  # {key: event_body}

# ── Event type defaults ───────────────────────────────────────────────────────

EVENT_TYPES = {
    "tennis":   {"emoji": "🎾", "duration": 120, "reminders": [1440, 60]},
    "match":    {"emoji": "🎾", "duration": 120, "reminders": [1440, 60]},
    "dinner":   {"emoji": "🍽️", "duration": 180, "reminders": [1440, 120]},
    "lunch":    {"emoji": "🍽️", "duration": 90,  "reminders": [1440, 120]},
    "call":     {"emoji": "📞", "duration": 30,  "reminders": [10]},
    "meeting":  {"emoji": "📅", "duration": 60,  "reminders": [10]},
    "flight":   {"emoji": "✈️", "duration": 180, "reminders": [1440, 120]},
    "deadline": {"emoji": "⚖️", "duration": 0,   "reminders": [4320, 480]},
    "reminder": {"emoji": "🔁", "duration": 30,  "reminders": [0]},
    "prep":     {"emoji": "📊", "duration": 60,  "reminders": [0]},
    "travel":   {"emoji": "✈️", "duration": 180, "reminders": [1440, 120]},
    "default":  {"emoji": "📅", "duration": 60,  "reminders": [60]},
}


class GCalSkill(BaseSkill):
    name        = "gcal"
    description = "Create Google Calendar events from any description or screenshot"
    commands    = ["/cal", "/event", "/gcal"]
    examples    = [
        "meeting with Kate tomorrow at 3pm for 1 hour",
        "tennis vs Marcus Saturday 10am",
        "dentist appointment Friday 10am",
        "lunch with Jess next Tuesday 12:30",
        "/cal team sync Thursday 2pm 45min",
    ]

    def __init__(self):
        self._service = None

    async def on_load(self):
        try:
            self._service = _get_calendar_service()
            print("[gcal] Google Calendar connected ✓")
        except Exception as e:
            print(f"[gcal] WARNING: {e}")

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                     user_text: str, extracted: dict | None = None) -> SkillResult:
        if not self._service:
            return SkillResult(
                "⚠️ Google Calendar not connected.\n"
                "Run `python -m skills.gcal.auth_setup` to authorise.",
                success=False,
            )

        ext = extracted or await _extract_event(user_text)

        if not ext.get("title"):
            return SkillResult(
                "I couldn't find an event title. Try:\n_meeting with Kate tomorrow at 3pm_",
                success=False,
            )

        preview, event_body = _build_preview(ext)
        key = f"{update.effective_user.id}:{datetime.now().timestamp()}"
        _pending[key] = event_body

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Create event", callback_data=f"gcal_confirm:{key}"),
            InlineKeyboardButton("✏️ Edit",          callback_data=f"gcal_edit:{key}"),
            InlineKeyboardButton("❌ Cancel",         callback_data=f"gcal_cancel:{key}"),
        ]])

        await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)
        return SkillResult("", success=True)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data  = query.data

        if data.startswith("gcal_cancel:"):
            _pending.pop(data.split(":", 1)[1], None)
            await query.edit_message_text("❌ Cancelled.")
            return

        if data.startswith("gcal_edit:"):
            await query.edit_message_text(
                "Tell me what to change — e.g. _make it 2 hours_, _change time to 4pm_, _add location Hyde Park_"
            )
            return

        if data.startswith("gcal_confirm:"):
            key = data.split(":", 1)[1]
            event_body = _pending.pop(key, None)
            if not event_body:
                await query.edit_message_text("⚠️ Event expired. Please try again.")
                return
            try:
                created = self._service.events().insert(
                    calendarId="primary", body=event_body
                ).execute()
                link = created.get("htmlLink", "")
                title = event_body.get("summary", "Event")
                await query.edit_message_text(
                    f"✅ *{title}* added to your calendar.\n\n[Open in Google Calendar]({link})",
                    parse_mode="Markdown",
                )
            except Exception as e:
                await query.edit_message_text(f"❌ Failed to create event: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _extract_event(user_text: str) -> dict:
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y")
    resp = _claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=f"""Today is {today}. Timezone: Europe/London.
Extract calendar event details. Respond ONLY with JSON, no markdown:
{{
  "title": string (descriptive, NO emoji — we add that),
  "event_type": one of: tennis|match|dinner|lunch|call|meeting|flight|deadline|reminder|prep|travel|default,
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM" or null for all-day,
  "duration_minutes": int or null (null = use type default),
  "all_day": bool,
  "location": string or null,
  "attendees": [string] or [],
  "description_context": string (raw context for description),
  "claude_note": string (actionable advice for the event)
}}""",
        messages=[{"role": "user", "content": user_text}],
    )
    raw = resp.content[0].text.strip().strip("```json").strip("```").strip()
    return json.loads(raw)


def _build_preview(ext: dict) -> tuple[str, dict]:
    tz = ZoneInfo(TIMEZONE)
    etype = ext.get("event_type", "default")
    defaults = EVENT_TYPES.get(etype, EVENT_TYPES["default"])

    emoji    = defaults["emoji"]
    duration = ext.get("duration_minutes") or defaults["duration"]
    reminder_mins: list[int] = defaults["reminders"]

    # Resolve date
    date_str = _resolve_date(ext.get("date", ""), tz)
    all_day  = ext.get("all_day", False) or duration == 0
    time_str = ext.get("start_time") or ("" if all_day else "09:00")

    if all_day:
        start_dt = datetime.fromisoformat(date_str).replace(tzinfo=tz)
        end_dt   = start_dt
        when_str = start_dt.strftime("%A, %d %B · All Day")
    else:
        start_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00").replace(tzinfo=tz)
        end_dt   = start_dt + timedelta(minutes=int(duration))
        when_str = f"{start_dt.strftime('%A, %d %B · %H:%M')} – {end_dt.strftime('%H:%M')}"

    title = f"{emoji} {ext['title']}"

    # Reminders human-readable
    def fmt_reminder(m: int) -> str:
        if m == 0:     return "At time of event"
        if m < 60:     return f"{m} min before"
        if m == 60:    return "1 hour before"
        if m < 1440:   return f"{m//60} hours before"
        if m == 1440:  return "1 day before"
        return f"{m//1440} days before"

    reminder_str = " + ".join(fmt_reminder(m) for m in reminder_mins)

    # Description
    desc_parts = []
    if ext.get("attendees"):
        desc_parts.append("Attendees: " + ", ".join(ext["attendees"]))
    if ext.get("description_context"):
        desc_parts.append(ext["description_context"])
    if ext.get("claude_note"):
        desc_parts.append(f"\n📝 Note from Claude:\n{ext['claude_note']}")
    description = "\n".join(desc_parts)

    # Preview markdown
    preview_lines = [
        "📋 *Event Preview*\n",
        f"🎯 *{title}*",
        f"- *When:* {when_str}",
    ]
    if ext.get("location"):
        preview_lines.append(f"- *Location:* {ext['location']}")
    preview_lines.append(f"- *Reminder(s):* {reminder_str}")
    if description:
        preview_lines.append(f"- *Description:*\n  > {description.replace(chr(10), chr(10)+'  > ')}")
    preview_lines.append("\nShall I go ahead and create this?")
    preview = "\n".join(preview_lines)

    # Google Calendar event body
    if all_day:
        event_body = {
            "summary": title,
            "start": {"date": date_str},
            "end":   {"date": date_str},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": m} for m in reminder_mins],
            },
        }
    else:
        event_body = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": m} for m in reminder_mins],
            },
        }
    if description:
        event_body["description"] = description
    if ext.get("location"):
        event_body["location"] = ext["location"]

    return preview, event_body


def _resolve_date(date_str: str, tz) -> str:
    today = datetime.now(tz).date()
    d = (date_str or "").lower().strip()
    if d in ("today", ""):   return str(today)
    if d == "tomorrow":      return str(today + timedelta(days=1))
    if d == "yesterday":     return str(today - timedelta(days=1))
    try:
        datetime.fromisoformat(date_str)
        return date_str
    except ValueError:
        return str(today)


def _get_calendar_service():
    creds = None
    b64 = os.getenv("GOOGLE_TOKEN_B64")
    if b64:
        creds = pickle.loads(base64.b64decode(b64))
    elif TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds:
        raise RuntimeError("No Google token found — run auth_setup or set GOOGLE_TOKEN_B64")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        print("[gcal] Token refreshed — update GOOGLE_TOKEN_B64 in Railway")
    if not creds.valid:
        raise RuntimeError("Google token invalid — re-run auth_setup")
    return build("calendar", "v3", credentials=creds)


_skill_instance = GCalSkill()
registry.register(_skill_instance)
