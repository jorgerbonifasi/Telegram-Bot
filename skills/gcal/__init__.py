"""
skills/gcal/__init__.py  —  Google Calendar Skill

Features:
- View today / tomorrow / week agenda  (/cal with no args)
- Create events from natural language
- Edit events via inline button (fixed)
- Delete events ("cancel my tennis tomorrow")
- Nav menu [📅 Today] [📅 Tomorrow] [🗓 Week] [➕ Add] on every response
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
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.skill_base import BaseSkill, SkillResult, registry

SCOPES     = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = Path("google_token.pickle")
TIMEZONE   = "Europe/London"

_claude  = anthropic.Anthropic()
_pending: dict[str, dict] = {}  # {key: {"body": event_body, "ext": ext}}
_REVOKED_MSG = (
    "⚠️ Google Calendar token has been revoked or expired.\n"
    "Re-run `python -m skills.gcal.auth_setup` and update GOOGLE\\_TOKEN\\_B64 in Railway."
)

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
    description = "View and manage your calendar"
    commands    = ["/cal", "/event", "/gcal"]
    examples    = [
        "meeting with Kate tomorrow at 3pm for 1 hour",
        "tennis vs Marcus Saturday 10am",
        "what's on today",
        "cancel my dentist appointment",
        "/cal",
    ]

    def __init__(self):
        self._service = None

    async def on_load(self):
        try:
            self._service = _get_calendar_service()
            print("[gcal] Google Calendar connected ✓")
        except Exception as e:
            print(f"[gcal] WARNING: {e}")

    # ── Main handler ──────────────────────────────────────────────────────────

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                     user_text: str, extracted: dict | None = None) -> SkillResult:
        if not self._service:
            return SkillResult(
                "⚠️ Google Calendar not connected.\n"
                "Run `python -m skills.gcal.auth_setup` to authorise.",
                success=False,
            )

        action = self._parse_gcal_action(user_text, extracted)

        if action == "view":
            period = self._parse_period(user_text)
            return await self._view_agenda(period)

        if action == "delete":
            return await self._delete_flow(update, context, user_text)

        # ── Create flow ───────────────────────────────────────────────────────
        ext = extracted or await _extract_event(user_text)

        if not ext.get("title"):
            return SkillResult(
                "I couldn't find an event title. Try:\n_meeting with Kate tomorrow at 3pm_",
                success=False,
                reply_markup=self._nav_keyboard(),
            )

        preview, event_body = _build_preview(ext)
        key = f"{update.effective_user.id}:{datetime.now().timestamp()}"
        _pending[key] = {"body": event_body, "ext": ext}

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Create event", callback_data=f"gcal_confirm:{key}"),
            InlineKeyboardButton("✏️ Edit",          callback_data=f"gcal_edit:{key}"),
            InlineKeyboardButton("❌ Cancel",         callback_data=f"gcal_cancel:{key}"),
        ]])

        await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)
        return SkillResult("", success=True)

    async def handle_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                          correction: str, key: str) -> SkillResult:
        """Re-extract event using correction + original ext, show updated preview."""
        pending = _pending.get(key)
        if not pending:
            return SkillResult("⚠️ Event expired — please describe it again.", success=False,
                               reply_markup=self._nav_keyboard())

        new_ext = await _re_extract_event(pending["ext"], correction)
        preview, event_body = _build_preview(new_ext)
        _pending[key] = {"body": event_body, "ext": new_ext}

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Create event", callback_data=f"gcal_confirm:{key}"),
            InlineKeyboardButton("✏️ Edit",          callback_data=f"gcal_edit:{key}"),
            InlineKeyboardButton("❌ Cancel",         callback_data=f"gcal_cancel:{key}"),
        ]])

        await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)
        return SkillResult("", success=True)

    # ── Callback handler ──────────────────────────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data  = query.data

        # ── Nav menu ──────────────────────────────────────────────────────────
        if data in ("gcal_today", "gcal_tomorrow", "gcal_week"):
            period = data.split("_", 1)[1]
            result = await self._view_agenda(period)
            await query.edit_message_text(result.text, parse_mode="Markdown",
                                          reply_markup=result.reply_markup)
            return

        if data == "gcal_add":
            context.user_data["gcal_state"] = "awaiting_create"
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="What's the event? _(e.g. tennis vs Marcus Saturday 10am)_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="gcal_add_cancel"),
                ]]),
            )
            return

        if data == "gcal_add_cancel":
            context.user_data.pop("gcal_state", None)
            await query.edit_message_text("Cancelled.")
            return

        # ── Create flow ───────────────────────────────────────────────────────
        if data.startswith("gcal_cancel:"):
            _pending.pop(data.split(":", 1)[1], None)
            await query.edit_message_text("❌ Cancelled.")
            return

        if data.startswith("gcal_edit:"):
            key = data.split(":", 1)[1]
            if key not in _pending:
                await query.edit_message_text("⚠️ Event expired. Please try again.")
                return
            context.user_data["gcal_state"] = {"mode": "editing", "key": key}
            await query.edit_message_text(
                "What would you like to change?\n"
                "_e.g. make it 2 hours · change to 4pm · add location Hyde Park_",
                parse_mode="Markdown",
            )
            return

        if data.startswith("gcal_confirm:"):
            key = data.split(":", 1)[1]
            pending = _pending.pop(key, None)
            if not pending:
                await query.edit_message_text("⚠️ Event expired. Please try again.")
                return
            try:
                created = self._service.events().insert(
                    calendarId="primary", body=pending["body"]
                ).execute()
                link  = created.get("htmlLink", "")
                title = pending["body"].get("summary", "Event")
                await query.edit_message_text(
                    f"✅ *{title}* added to your calendar.\n\n[Open in Google Calendar]({link})",
                    parse_mode="Markdown",
                )
            except RefreshError:
                self._service = None
                await query.edit_message_text(_REVOKED_MSG, parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ Failed to create event: {e}")
            return

        # ── Delete flow ───────────────────────────────────────────────────────
        if data.startswith("gcal_delete_confirm:"):
            event_id = data.split(":", 1)[1]
            try:
                self._service.events().delete(
                    calendarId="primary", eventId=event_id
                ).execute()
                await query.edit_message_text("🗑 Event deleted.")
            except RefreshError:
                self._service = None
                await query.edit_message_text(_REVOKED_MSG, parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ Failed to delete: {e}")
            return

        if data == "gcal_delete_cancel":
            await query.edit_message_text("Cancelled.")
            return

        # ── Update flow (from photo) ──────────────────────────────────────────
        if data.startswith("gcal_update_confirm:"):
            key     = data.split(":", 1)[1]
            pending = _pending.pop(key, None)
            if not pending:
                await query.edit_message_text("⚠️ Event expired. Please try again.")
                return
            event_id = pending.get("event_id")
            try:
                if event_id:
                    updated = self._service.events().patch(
                        calendarId="primary", eventId=event_id, body=pending["body"]
                    ).execute()
                    link  = updated.get("htmlLink", "")
                    title = pending["body"].get("summary", "Event")
                    await query.edit_message_text(
                        f"✅ *{title}* updated in your calendar.\n\n[Open in Google Calendar]({link})",
                        parse_mode="Markdown",
                    )
                else:
                    created = self._service.events().insert(
                        calendarId="primary", body=pending["body"]
                    ).execute()
                    link  = created.get("htmlLink", "")
                    title = pending["body"].get("summary", "Event")
                    await query.edit_message_text(
                        f"✅ *{title}* added to your calendar.\n\n[Open in Google Calendar]({link})",
                        parse_mode="Markdown",
                    )
            except RefreshError:
                self._service = None
                await query.edit_message_text(_REVOKED_MSG, parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ Failed: {e}")
            return

    # ── Photo handler ─────────────────────────────────────────────────────────

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                           photo_bytes: bytes, caption: str = "") -> SkillResult:
        """Extract event from screenshot, find existing match, show preview."""
        if not self._service:
            return SkillResult("⚠️ Google Calendar not connected.", success=False)

        ext = await _extract_event_from_image(photo_bytes, caption)

        if not ext.get("title"):
            return SkillResult(
                "I couldn't spot event details in that image. Try describing the event in text.",
                success=False, reply_markup=self._nav_keyboard(),
            )

        preview, event_body = _build_preview(ext)
        key = f"{update.effective_user.id}:{datetime.now().timestamp()}"

        # Detect update intent from caption alone (don't require an existing match)
        update_keywords = ["update", "edit", "change", "modify", "current", "move", "reschedule", "amend"]
        update_intent = any(w in caption.lower() for w in update_keywords)

        # Search for an existing calendar event on the same date (title may differ)
        existing = self._find_event_on_date(ext.get("date", "")) if update_intent else None

        if update_intent:
            event_id    = existing["id"] if existing else None
            found_title = existing.get("summary") if existing else None
            _pending[key] = {"body": event_body, "ext": ext, "event_id": event_id}

            if found_title:
                header = (
                    f"📋 *Update Preview*\n\n"
                    f"Found on that date: *{found_title}*\n"
                    f"↓ Will be updated to:\n\n"
                )
            else:
                header = (
                    f"📋 *Event Preview*\n\n"
                    f"_No existing event found on that date — will create a new one._\n\n"
                )

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Update event" if found_title else "✅ Create event",
                    callback_data=f"gcal_update_confirm:{key}",
                ),
                InlineKeyboardButton("❌ Cancel", callback_data=f"gcal_cancel:{key}"),
            ]])
            await update.message.reply_text(
                header + preview, parse_mode="Markdown", reply_markup=keyboard
            )
        else:
            _pending[key] = {"body": event_body, "ext": ext}
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Create event", callback_data=f"gcal_confirm:{key}"),
                InlineKeyboardButton("✏️ Edit",          callback_data=f"gcal_edit:{key}"),
                InlineKeyboardButton("❌ Cancel",         callback_data=f"gcal_cancel:{key}"),
            ]])
            await update.message.reply_text(
                preview, parse_mode="Markdown", reply_markup=keyboard
            )

        return SkillResult("", success=True)

    # ── Event search ──────────────────────────────────────────────────────────

    def _find_event_by_title(self, title: str, date_str: str = "") -> dict | None:
        """Search upcoming events for a title match."""
        if not title or not self._service:
            return None
        try:
            tz  = ZoneInfo(TIMEZONE)
            now = datetime.now(tz)
            result = self._service.events().list(
                calendarId  = "primary",
                timeMin     = now.isoformat(),
                timeMax     = (now + timedelta(days=90)).isoformat(),
                singleEvents= True,
                orderBy     = "startTime",
                maxResults  = 20,
                q           = title,
            ).execute()
            events = result.get("items", [])
            return events[0] if events else None
        except RefreshError:
            self._service = None
            return None
        except Exception as e:
            print(f"[gcal] Event search error: {e}")
            return None

    def _find_event_on_date(self, date_str: str) -> dict | None:
        """Return the first calendar event on a given date (any title)."""
        if not date_str or not self._service:
            return None
        try:
            tz        = ZoneInfo(TIMEZONE)
            date_str  = _resolve_date(date_str, tz)
            day_start = datetime.fromisoformat(date_str).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=tz
            )
            day_end   = day_start + timedelta(days=1)
            result    = self._service.events().list(
                calendarId  = "primary",
                timeMin     = day_start.isoformat(),
                timeMax     = day_end.isoformat(),
                singleEvents= True,
                orderBy     = "startTime",
                maxResults  = 5,
            ).execute()
            events = result.get("items", [])
            return events[0] if events else None
        except RefreshError:
            self._service = None
            return None
        except Exception as e:
            print(f"[gcal] Date event search error: {e}")
            return None

    # ── View agenda ───────────────────────────────────────────────────────────

    async def _view_agenda(self, period: str = "today") -> SkillResult:
        tz  = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)

        if period == "tomorrow":
            day_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end   = day_start + timedelta(days=1)
            header    = day_start.strftime("📅 *Tomorrow — %A %d %b*")
        elif period == "week":
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end   = day_start + timedelta(days=7)
            header    = "🗓 *Next 7 Days*"
        else:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end   = day_start + timedelta(days=1)
            header    = now.strftime("📅 *Today — %A %d %b*")

        try:
            events_result = self._service.events().list(
                calendarId  = "primary",
                timeMin     = day_start.isoformat(),
                timeMax     = day_end.isoformat(),
                singleEvents= True,
                orderBy     = "startTime",
                maxResults  = 20,
            ).execute()
        except RefreshError:
            self._service = None
            return SkillResult(_REVOKED_MSG, success=False)

        events = events_result.get("items", [])
        lines  = [header, ""]

        if not events:
            lines.append("_Nothing scheduled_")
        elif period == "week":
            current_day = None
            for ev in events:
                start_raw = ev["start"].get("dateTime", ev["start"].get("date"))
                if "T" in start_raw:
                    dt        = datetime.fromisoformat(start_raw).astimezone(tz)
                    day_label = dt.strftime("%A %d %b")
                    time_str  = f"  · {self._fmt_time_range(ev, tz)}"
                else:
                    dt        = datetime.fromisoformat(start_raw)
                    day_label = dt.strftime("%A %d %b")
                    time_str  = "  · All day"

                if day_label != current_day:
                    if current_day:
                        lines.append("")
                    lines.append(f"*{day_label}*")
                    current_day = day_label

                lines.append(f"{ev.get('summary', 'Untitled')}{time_str}")
        else:
            for ev in events:
                lines.append(f"{ev.get('summary', 'Untitled')}  · {self._fmt_time_range(ev, tz)}")

        lines.append("")
        n = len(events)
        if n == 1:
            lines.append("_1 event_")
        elif n > 1:
            lines.append(f"_{n} events_")

        return SkillResult("\n".join(lines), reply_markup=self._nav_keyboard())

    # ── Delete flow ───────────────────────────────────────────────────────────

    async def _delete_flow(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                           user_text: str) -> SkillResult:
        tz  = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        q   = self._extract_delete_query(user_text)

        try:
            events_result = self._service.events().list(
                calendarId  = "primary",
                timeMin     = now.isoformat(),
                timeMax     = (now + timedelta(days=30)).isoformat(),
                singleEvents= True,
                orderBy     = "startTime",
                maxResults  = 10,
                q           = q,
            ).execute()
        except RefreshError:
            self._service = None
            return SkillResult(_REVOKED_MSG, success=False)

        events = events_result.get("items", [])

        if not events:
            return SkillResult(
                f"Couldn't find any upcoming events matching _{q}_",
                success=False,
                reply_markup=self._nav_keyboard(),
            )

        ev       = events[0]
        summary  = ev.get("summary", "Untitled")
        time_str = self._fmt_time_range(ev, tz)
        note     = f"\n_({len(events)} matches found, showing soonest)_" if len(events) > 1 else ""

        return SkillResult(
            f"🗑 Delete *{summary}*?\n_{time_str}_{note}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Yes, delete", callback_data=f"gcal_delete_confirm:{ev['id']}"),
                InlineKeyboardButton("❌ Cancel",       callback_data="gcal_delete_cancel"),
            ]]),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _nav_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 Today",    callback_data="gcal_today"),
            InlineKeyboardButton("📅 Tomorrow", callback_data="gcal_tomorrow"),
            InlineKeyboardButton("🗓 Week",     callback_data="gcal_week"),
            InlineKeyboardButton("➕ Add",      callback_data="gcal_add"),
        ]])

    def _fmt_time_range(self, event: dict, tz) -> str:
        start_raw = event["start"].get("dateTime", event["start"].get("date"))
        end_raw   = event["end"].get("dateTime",   event["end"].get("date"))
        if "T" not in start_raw:
            return "All day"
        start = datetime.fromisoformat(start_raw).astimezone(tz)
        end   = datetime.fromisoformat(end_raw).astimezone(tz)
        return f"{start.strftime('%H:%M')} – {end.strftime('%H:%M')}"

    def _parse_gcal_action(self, text: str, extracted: dict | None) -> str:
        t = text.lower().strip()
        if not t or t in ("today", "tomorrow", "this week", "week"):
            return "view"
        if any(w in t for w in ["what's on", "show", "agenda", "schedule", "what do i have", "what have i got"]):
            return "view"
        if any(t.startswith(w) for w in ["cancel my", "cancel ", "delete my", "delete ", "remove my", "remove "]):
            return "delete"
        return "create"

    def _parse_period(self, text: str) -> str:
        t = text.lower()
        if "tomorrow" in t: return "tomorrow"
        if "week" in t:     return "week"
        return "today"

    def _extract_delete_query(self, text: str) -> str:
        t = text.lower().strip()
        for prefix in ["cancel my ", "cancel ", "delete my ", "delete ", "remove my ", "remove "]:
            if t.startswith(prefix):
                t = t[len(prefix):]
                break
        for suffix in [" tomorrow", " today", " this week", " next week"]:
            if t.endswith(suffix):
                t = t[:-len(suffix)]
        return t.strip()


# ── Module-level helpers ──────────────────────────────────────────────────────

async def _extract_event_from_image(photo_bytes: bytes, caption: str = "") -> dict:
    """Use Claude vision to extract event details from a screenshot."""
    import base64
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y")
    now   = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")

    image_b64 = base64.standard_b64encode(photo_bytes).decode()

    user_content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
        },
        {
            "type": "text",
            "text": (f"User caption: {caption}\n\n" if caption else "") +
                    "Extract the calendar event details visible in this image.",
        },
    ]

    resp = _claude.messages.create(
        model      = "claude-sonnet-4-20250514",
        max_tokens = 400,
        system     = f"""Today is {today}. Current time: {now}. Timezone: Europe/London.
Extract calendar event details from the image.

CRITICAL DATE RULES:
- If the image contains an explicit date number (e.g. "25", "April 25", "25 de abril", "sábado 25"), use that exact date directly — do NOT recompute from the day name.
- Only derive the date from the day name alone (e.g. "next Saturday") when there is NO explicit date number in the image.
- If both a date number AND a day name appear, use the date number. The day name is just a sanity check.
- NEVER override an explicit numeric date by counting weekdays from today.

Respond ONLY with JSON, no markdown:
{{
  "title": string (descriptive, NO emoji),
  "event_type": one of: tennis|match|dinner|lunch|call|meeting|flight|deadline|reminder|prep|travel|default,
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM" or null,
  "duration_minutes": int or null,
  "all_day": bool,
  "location": string or null,
  "attendees": [string] or [],
  "description_context": string (key info from the image),
  "claude_note": string (anything useful to remember about this event)
}}""",
        messages = [{"role": "user", "content": user_content}],
    )
    raw = resp.content[0].text.strip().strip("```json").strip("```").strip()
    return json.loads(raw)


async def _extract_event(user_text: str) -> dict:
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y")
    now   = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")
    resp  = _claude.messages.create(
        model      = "claude-sonnet-4-20250514",
        max_tokens = 400,
        system     = f"""Today is {today}. Current time: {now}. Timezone: Europe/London.
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
        messages   = [{"role": "user", "content": user_text}],
    )
    raw = resp.content[0].text.strip().strip("```json").strip("```").strip()
    return json.loads(raw)


async def _re_extract_event(original_ext: dict, correction: str) -> dict:
    """Apply a user correction to an existing event extraction."""
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y")
    now   = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M")
    resp  = _claude.messages.create(
        model      = "claude-sonnet-4-20250514",
        max_tokens = 400,
        system     = f"""Today is {today}. Current time: {now}. Timezone: Europe/London.
You have an existing calendar event and a correction from the user.
Apply the correction and return the updated event as JSON (same schema, no markdown):
{{
  "title": string, "event_type": string, "date": "YYYY-MM-DD",
  "start_time": "HH:MM" or null, "duration_minutes": int or null,
  "all_day": bool, "location": string or null, "attendees": [string],
  "description_context": string, "claude_note": string
}}""",
        messages   = [{"role": "user", "content":
            f"Original event:\n{json.dumps(original_ext, indent=2)}\n\nUser correction: {correction}"}],
    )
    raw = resp.content[0].text.strip().strip("```json").strip("```").strip()
    return json.loads(raw)


def _build_preview(ext: dict) -> tuple[str, dict]:
    tz = ZoneInfo(TIMEZONE)
    etype    = ext.get("event_type", "default")
    defaults = EVENT_TYPES.get(etype, EVENT_TYPES["default"])

    emoji          = defaults["emoji"]
    duration       = ext.get("duration_minutes") or defaults["duration"]
    reminder_mins  = defaults["reminders"]

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

    def fmt_reminder(m: int) -> str:
        if m == 0:    return "At time of event"
        if m < 60:    return f"{m} min before"
        if m == 60:   return "1 hour before"
        if m < 1440:  return f"{m//60} hours before"
        if m == 1440: return "1 day before"
        return f"{m//1440} days before"

    reminder_str = " + ".join(fmt_reminder(m) for m in reminder_mins)

    desc_parts = []
    if ext.get("attendees"):
        desc_parts.append("Attendees: " + ", ".join(ext["attendees"]))
    if ext.get("description_context"):
        desc_parts.append(ext["description_context"])
    if ext.get("claude_note"):
        desc_parts.append(f"\n📝 Note from Claude:\n{ext['claude_note']}")
    description = "\n".join(desc_parts)

    preview_lines = [
        "📋 *Event Preview*\n",
        f"*{title}*",
        f"- *When:* {when_str}",
    ]
    if ext.get("location"):
        preview_lines.append(f"- *Location:* {ext['location']}")
    preview_lines.append(f"- *Reminder(s):* {reminder_str}")
    if description:
        preview_lines.append(f"- *Description:*\n  > {description.replace(chr(10), chr(10)+'  > ')}")
    preview_lines.append("\nShall I go ahead and create this?")

    if all_day:
        event_body = {
            "summary": title,
            "start":   {"date": date_str},
            "end":     {"date": date_str},
            "reminders": {"useDefault": False,
                          "overrides": [{"method": "popup", "minutes": m} for m in reminder_mins]},
        }
    else:
        event_body = {
            "summary": title,
            "start":   {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end":     {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
            "reminders": {"useDefault": False,
                          "overrides": [{"method": "popup", "minutes": m} for m in reminder_mins]},
        }
    if description:
        event_body["description"] = description
    if ext.get("location"):
        event_body["location"] = ext["location"]

    return "\n".join(preview_lines), event_body


def _resolve_date(date_str: str, tz) -> str:
    today = datetime.now(tz).date()
    d = (date_str or "").lower().strip()
    if d in ("today", ""):  return str(today)
    if d == "tomorrow":     return str(today + timedelta(days=1))
    if d == "yesterday":    return str(today - timedelta(days=1))
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
        try:
            creds.refresh(Request())
            print("[gcal] Token refreshed — update GOOGLE_TOKEN_B64 in Railway")
        except RefreshError as e:
            raise RuntimeError(
                f"Google token has been revoked or expired — re-run auth_setup ({e})"
            ) from e
    if not creds.valid:
        raise RuntimeError("Google token invalid — re-run auth_setup")
    return build("calendar", "v3", credentials=creds)


_skill_instance = GCalSkill()
registry.register(_skill_instance)
