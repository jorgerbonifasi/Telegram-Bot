"""
skills/habits/__init__.py  —  Habit Tracker Skill

Connects to Lovable Habit Tracker via Supabase edge function API.

Habits:
  tooth_brushing  — 3x per day (Morning / Lunch / Night)
  healthy_eating  — 3 meals per day (Breakfast / Lunch / Dinner)
  water_intake    — 3000 mL per day
  exercise        — workout completed
  mouth_guard     — completed
  steps           — 15,000 per day
  social_media    — under 45 min per day

Commands: /habits  /habit  /h
"""

from __future__ import annotations
import os
import re
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.skill_base import BaseSkill, SkillResult, registry

TIMEZONE = "Europe/London"
_API_URL = os.getenv(
    "HABITS_API_URL",
    "https://vrtnchnjxevjivgridav.supabase.co/functions/v1/external-api",
)
_API_KEY = os.getenv("HABITS_API_KEY", "")

# ── Habit definitions ──────────────────────────────────────────────────────────

HABITS: dict[str, dict] = {
    "tooth_brushing": {
        "label": "Tooth Brushing", "emoji": "🦷",
        "type": "multi_bool", "slots": ["Morning", "Lunch", "Night"],
    },
    "healthy_eating": {
        "label": "Healthy Eating", "emoji": "🍎",
        "type": "multi_bool", "slots": ["Breakfast", "Lunch", "Dinner"],
    },
    "water_intake": {
        "label": "Water Intake", "emoji": "💧",
        "type": "numeric", "unit": "mL", "target": 3000,
        "increments": [250, 500, 1000],
    },
    "exercise": {
        "label": "Exercise", "emoji": "🏃",
        "type": "bool",
    },
    "mouth_guard": {
        "label": "Mouth Guard", "emoji": "🛡",
        "type": "bool",
    },
    "steps": {
        "label": "Steps", "emoji": "👟",
        "type": "numeric", "unit": "steps", "target": 15000,
        "increments": [1000, 2500, 5000],
    },
    "social_media": {
        "label": "Social Media", "emoji": "📱",
        "type": "numeric", "unit": "min", "target": 45,
        "increments": [15, 30, 45],
        "less_is_better": True,
    },
}

# ── API helpers ────────────────────────────────────────────────────────────────

def _today(tz=None) -> str:
    return str(datetime.now(tz or ZoneInfo(TIMEZONE)).date())


def _headers() -> dict:
    return {"x-api-key": _API_KEY, "Content-Type": "application/json"}


async def _fetch_entries(date_key: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            _API_URL,
            params={"date_key": date_key, "limit": 50},
            headers=_headers(),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])


async def _upsert(habit_id: str, value, date_key: str) -> None:
    entries  = await _fetch_entries(date_key)
    existing = next((e for e in entries if e["habit_id"] == habit_id), None)
    async with httpx.AsyncClient() as client:
        if existing:
            r = await client.patch(
                f"{_API_URL}/{existing['id']}",
                json={"value": value},
                headers=_headers(),
                timeout=10,
            )
        else:
            r = await client.post(
                _API_URL,
                json={"date_key": date_key, "habit_id": habit_id, "value": value},
                headers=_headers(),
                timeout=10,
            )
        r.raise_for_status()

# ── Value helpers ──────────────────────────────────────────────────────────────

def _parse_val(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def _as_bool_list(val, length: int) -> list[bool]:
    if isinstance(val, list):
        result = [bool(v) for v in val]
        while len(result) < length:
            result.append(False)
        return result
    return [False] * length


def _as_number(val) -> int | float:
    if isinstance(val, (int, float)):
        return val
    try:
        return float(val)
    except Exception:
        return 0

# ── Rendering ──────────────────────────────────────────────────────────────────

def _render_habit(habit_id: str, entry: dict | None) -> str:
    h   = HABITS[habit_id]
    raw = _parse_val(entry["value"] if entry else None)

    if h["type"] == "multi_bool":
        vals   = _as_bool_list(raw, len(h["slots"]))
        checks = " ".join("✅" if v else "○" for v in vals)
        done   = sum(vals)
        total  = len(h["slots"])
        return f"{h['emoji']} *{h['label']}*: {checks}  ({done}/{total})"

    if h["type"] == "bool":
        done = bool(raw) if raw is not None else False
        return f"{h['emoji']} *{h['label']}*: {'✅' if done else '○'}"

    # numeric
    cur    = _as_number(raw)
    target = h["target"]
    unit   = h.get("unit", "")
    less   = h.get("less_is_better", False)
    if less:
        ok = cur <= target
        return f"{h['emoji']} *{h['label']}*: {'✅' if ok else '⚠️'} {int(cur)}/{target} {unit}"
    filled = min(10, int(cur / target * 10)) if target else 0
    bar    = "▓" * filled + "░" * (10 - filled)
    return f"{h['emoji']} *{h['label']}*: {int(cur):,}/{target:,} {unit}\n    `{bar}`"


def _daily_score(entries: list[dict]) -> int:
    by_id  = {e["habit_id"]: e for e in entries}
    total, done = 0, 0
    for hid, h in HABITS.items():
        val = _parse_val(by_id[hid]["value"] if hid in by_id else None)
        if h["type"] == "multi_bool":
            n      = len(h["slots"])
            vals   = _as_bool_list(val, n)
            total += n
            done  += sum(vals)
        elif h["type"] == "bool":
            total += 1
            done  += 1 if val else 0
        else:
            target = h["target"]
            cur    = _as_number(val)
            total += target
            if h.get("less_is_better"):
                done += target if cur <= target else max(0, 2 * target - cur)
            else:
                done += min(cur, target)
    return int(done / total * 100) if total else 0

# ── Inline keyboard ────────────────────────────────────────────────────────────

def _build_keyboard(by_id: dict) -> InlineKeyboardMarkup:
    rows = []

    for hid in ("tooth_brushing", "healthy_eating"):
        h     = HABITS[hid]
        entry = by_id.get(hid)
        vals  = _as_bool_list(_parse_val(entry["value"] if entry else None), len(h["slots"]))
        row   = [
            InlineKeyboardButton(
                f"{'✅' if vals[i] else '○'} {slot}",
                callback_data=f"habits_toggle:{hid}:{i}",
            )
            for i, slot in enumerate(h["slots"])
        ]
        rows.append(row)

    bool_row = []
    for hid in ("exercise", "mouth_guard"):
        h    = HABITS[hid]
        entry = by_id.get(hid)
        done  = bool(_parse_val(entry["value"] if entry else None))
        bool_row.append(InlineKeyboardButton(
            f"{'✅' if done else '○'} {h['emoji']} {h['label']}",
            callback_data=f"habits_bool:{hid}",
        ))
    rows.append(bool_row)

    for hid in ("water_intake", "steps", "social_media"):
        h   = HABITS[hid]
        row = [
            InlineKeyboardButton(
                f"{h['emoji']} +{inc:,} {h['unit']}",
                callback_data=f"habits_add:{hid}:{inc}",
            )
            for inc in h.get("increments", [])
        ]
        rows.append(row)

    return InlineKeyboardMarkup(rows)

# ── Skill class ────────────────────────────────────────────────────────────────

class HabitsSkill(BaseSkill):
    name        = "habits"
    description = "Track your daily habits"
    commands    = ["/habits", "/habit", "/h"]
    examples    = [
        "show habits",
        "8000 steps",
        "morning tooth brushing",
        "500ml water",
        "did exercise",
        "mouth guard done",
        "30 min social media",
    ]

    async def on_load(self):
        if not _API_KEY:
            print("[habits] WARNING: HABITS_API_KEY not set")
        else:
            print("[habits] Habit Tracker connected ✓")

    # ── Main handler ────────────────────────────────────────────────────────────

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                     user_text: str, extracted: dict | None = None) -> SkillResult:
        t = user_text.lower().strip()
        if not t or t in ("show", "view", "today", "list"):
            return await self._view()

        logged = await self._parse_and_log(t, user_text)
        if logged:
            return await self._view(header=f"✅ Logged: {logged}")
        return await self._view()

    # ── Callback handler ────────────────────────────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data  = query.data

        try:
            if data == "habits_view":
                pass  # falls through to refresh below

            elif data.startswith("habits_toggle:"):
                _, hid, slot_str = data.split(":", 2)
                await self._toggle_slot(hid, int(slot_str))

            elif data.startswith("habits_bool:"):
                hid = data.split(":", 1)[1]
                await self._toggle_bool(hid)

            elif data.startswith("habits_add:"):
                parts  = data.split(":")
                hid, amount = parts[1], int(parts[2])
                await self._add_numeric(hid, amount)

            result = await self._view()
            await query.edit_message_text(
                result.text, parse_mode="Markdown", reply_markup=result.reply_markup
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")

    # ── View ────────────────────────────────────────────────────────────────────

    async def _view(self, header: str = "") -> SkillResult:
        tz      = ZoneInfo(TIMEZONE)
        today   = _today(tz)
        date_lbl = datetime.now(tz).strftime("%A %d %b")
        try:
            entries = await _fetch_entries(today)
        except Exception as e:
            return SkillResult(f"❌ Could not load habits: {e}", success=False)

        by_id = {e["habit_id"]: e for e in entries}
        score = _daily_score(entries)

        lines = []
        if header:
            lines += [header, ""]
        lines += [f"📊 *Habits — {date_lbl}*", f"Daily score: *{score}%*", ""]
        for hid in HABITS:
            lines.append(_render_habit(hid, by_id.get(hid)))

        return SkillResult("\n".join(lines), reply_markup=_build_keyboard(by_id))

    # ── Mutations ───────────────────────────────────────────────────────────────

    async def _toggle_slot(self, habit_id: str, slot: int) -> None:
        today   = _today()
        entries = await _fetch_entries(today)
        entry   = next((e for e in entries if e["habit_id"] == habit_id), None)
        n       = len(HABITS[habit_id]["slots"])
        vals    = _as_bool_list(_parse_val(entry["value"] if entry else None), n)
        vals[slot] = not vals[slot]
        await _upsert(habit_id, vals, today)

    async def _toggle_bool(self, habit_id: str) -> None:
        today   = _today()
        entries = await _fetch_entries(today)
        entry   = next((e for e in entries if e["habit_id"] == habit_id), None)
        current = bool(_parse_val(entry["value"] if entry else None))
        await _upsert(habit_id, not current, today)

    async def _add_numeric(self, habit_id: str, amount: int | float) -> None:
        today   = _today()
        entries = await _fetch_entries(today)
        entry   = next((e for e in entries if e["habit_id"] == habit_id), None)
        current = _as_number(_parse_val(entry["value"] if entry else None))
        await _upsert(habit_id, current + amount, today)

    # ── Natural language parsing ─────────────────────────────────────────────────

    async def _parse_and_log(self, t: str, original: str) -> str | None:
        # Steps
        m = re.search(r'(\d[\d,]*)\s*steps?', t)
        if m:
            n = int(m.group(1).replace(",", ""))
            await self._add_numeric("steps", n)
            return f"{n:,} steps"

        # Water — e.g. "500ml", "1000 ml", "1l", "1.5l"
        m = re.search(r'(\d+\.?\d*)\s*(ml|l\b)', t)
        if m:
            n = float(m.group(1))
            if m.group(2) == "l":
                n *= 1000
            await self._add_numeric("water_intake", int(n))
            return f"{int(n)} mL water"

        # Social media — only if "social", "media", or "phone" in text
        if any(w in t for w in ("social", "media", "phone", "screen")):
            m = re.search(r'(\d+)\s*min', t)
            if m:
                n = int(m.group(1))
                await self._add_numeric("social_media", n)
                return f"{n} min social media"

        # Tooth brushing slots
        brushing_words = ("brush", "tooth", "teeth", "brushing")
        slot_map = {"morning": 0, "lunch": 1, "night": 2}
        if any(w in t for w in brushing_words):
            for slot_name, idx in slot_map.items():
                if slot_name in t:
                    await self._toggle_slot("tooth_brushing", idx)
                    return f"{slot_name.capitalize()} tooth brushing"

        # Healthy eating slots
        eating_words = ("eat", "meal", "food", "healthy eating", "ate")
        meal_map = {"breakfast": 0, "lunch": 1, "dinner": 2}
        if any(w in t for w in eating_words):
            for meal, idx in meal_map.items():
                if meal in t:
                    await self._toggle_slot("healthy_eating", idx)
                    return f"{meal.capitalize()} meal"

        # Exercise
        if any(w in t for w in ("exercise", "workout", "gym", "ran", "run", "trained", "training")):
            await self._toggle_bool("exercise")
            return "Exercise"

        # Mouth guard
        if any(w in t for w in ("mouth guard", "mouthguard", "guard")):
            await self._toggle_bool("mouth_guard")
            return "Mouth guard"

        return None


_skill_instance = HabitsSkill()
registry.register(_skill_instance)
