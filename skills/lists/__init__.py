"""
skills/lists/__init__.py  —  Multi-List Manager

Commands: /list, /l

Examples:
  /list                         → show all lists
  /list groceries               → open groceries list
  add 2x milk to groceries      → add item with quantity
  add passport to packing       → add item without quantity
  done milk                     → remove item (searched across lists)
  clear groceries               → wipe a list (with confirm)

Supabase table: lists
  create table lists (
    id         uuid primary key default gen_random_uuid(),
    user_id    bigint not null,
    list_name  text not null,
    item       text not null,
    quantity   text default '',
    created_at timestamptz default now()
  );
  create index on lists(user_id, list_name);
"""

from __future__ import annotations
import os
import re
from supabase import create_client, Client
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from core.skill_base import BaseSkill, SkillResult, registry

LIST_EMOJI: dict[str, str] = {
    "groceries": "🛒", "grocery": "🛒", "shopping": "🛒",
    "packing":   "🧳", "travel":  "🧳", "suitcase": "🧳",
    "amazon":    "📦", "online":  "📦",
    "books":     "📚", "reading": "📚",
    "movies":    "🎬", "films":   "🎬", "watchlist": "🎬",
    "pharmacy":  "💊", "medicine":"💊",
    "hardware":  "🔧", "diy":     "🔧",
    "work":      "💼", "office":  "💼",
}

def _emoji(name: str) -> str:
    return LIST_EMOJI.get(name.lower(), "📋")


class ListSkill(BaseSkill):
    name        = "lists"
    description = "Multi-list manager — groceries, packing, shopping and more"
    commands    = ["/list", "/l"]
    examples    = [
        "add 2x milk to groceries",
        "add passport to packing",
        "/list groceries",
        "done milk",
        "clear groceries",
    ]

    def __init__(self):
        self._db: Client | None = None

    async def on_load(self):
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if url and key:
            self._db = create_client(url, key)
        else:
            print("[lists] WARNING: Supabase not configured")

    # ── Main handler ──────────────────────────────────────────────────────────

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                     user_text: str, extracted: dict | None = None) -> SkillResult:
        if not self._db:
            return SkillResult("⚠️ Lists skill not configured.", success=False)

        uid = update.effective_user.id
        ext = extracted or {}
        action = ext.get("action") or self._parse_action(user_text)

        if action == "show_all" or not user_text.strip():
            return await self._render_all(uid)

        if action == "show":
            list_name = ext.get("list_name") or self._parse_list_name(user_text)
            return await self._render_list(uid, list_name)

        if action == "add":
            # Multi-item add: "separately", newlines, or comma-separated list
            items, list_name = self._parse_multi_items(user_text, ext)
            if items:
                for item_text in items:
                    item, qty = self._parse_quantity(item_text)
                    if item:
                        self._db.table("lists").insert({
                            "user_id": uid, "list_name": list_name,
                            "item": item, "quantity": qty,
                        }).execute()
                n = len([i for i in items if i])
                return await self._render_list(
                    uid, list_name,
                    prefix=f"✅ Added {n} items to {_emoji(list_name)} {list_name.title()}"
                )

            item, qty, list_name = self._parse_add(user_text, ext)
            if not item:
                return SkillResult("What would you like to add, and to which list?", success=False)
            return await self._add_item(uid, list_name, item, qty)

        if action == "done":
            item_text = ext.get("item") or self._parse_item_name(user_text)
            list_name = ext.get("list_name") or self._parse_list_name(user_text)
            return await self._remove_item(uid, list_name, item_text)

        if action == "clear":
            list_name = ext.get("list_name") or self._parse_list_name(user_text)
            return await self._confirm_clear(uid, list_name)

        # Fallback: treat user_text as a list name
        return await self._render_list(uid, user_text.strip().lower())

    # ── Callback handler ──────────────────────────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        parts     = query.data.split(":", 2)
        action    = parts[1] if len(parts) > 1 else ""
        list_name = parts[2] if len(parts) > 2 else ""
        uid       = update.effective_user.id

        if action == "all":
            result = await self._render_all(uid)
            await query.edit_message_text(result.text, parse_mode="Markdown",
                                          reply_markup=result.reply_markup)
            return

        if action == "show":
            result = await self._render_list(uid, list_name)
            await query.edit_message_text(result.text, parse_mode="Markdown",
                                          reply_markup=result.reply_markup)
            return

        if action == "add":
            context.user_data["list_state"] = {"mode": "awaiting_add", "list": list_name}
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"What to add to *{list_name.title()}*?\n"
                     f"_Include quantity if needed — e.g. 2x milk, 500g chicken_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="list:cancel"),
                ]]),
            )
            return

        if action == "done":
            context.user_data["list_state"] = {"mode": "awaiting_done", "list": list_name}
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Which item from *{list_name.title()}* is done?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="list:cancel"),
                ]]),
            )
            return

        if action == "clear":
            result = await self._confirm_clear(uid, list_name)
            await query.edit_message_text(result.text, parse_mode="Markdown",
                                          reply_markup=result.reply_markup)
            return

        if action == "clear_confirm":
            self._db.table("lists").delete().eq("user_id", uid).eq("list_name", list_name).execute()
            result = await self._render_all(uid)
            await query.edit_message_text(
                f"🗑 *{list_name.title()}* cleared.\n\n" + result.text,
                parse_mode="Markdown",
                reply_markup=result.reply_markup,
            )
            return

        if action == "new":
            context.user_data["list_state"] = {"mode": "awaiting_new"}
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="What should the new list be called?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="list:cancel"),
                ]]),
            )
            return

        if action == "cancel":
            context.user_data.pop("list_state", None)
            await query.edit_message_text("Cancelled.")
            return

    # ── Actions ───────────────────────────────────────────────────────────────

    async def _add_item(self, uid: int, list_name: str, item: str, quantity: str = "") -> SkillResult:
        self._db.table("lists").insert({
            "user_id":   uid,
            "list_name": list_name,
            "item":      item,
            "quantity":  quantity,
        }).execute()
        qty_str = f" ({quantity})" if quantity else ""
        return await self._render_list(
            uid, list_name,
            prefix=f"✅ Added *{item}*{qty_str} to {_emoji(list_name)} {list_name.title()}"
        )

    async def _remove_item(self, uid: int, list_name: str, item_text: str) -> SkillResult:
        if not item_text:
            return SkillResult("Which item?", success=False)

        rows = self._fuzzy_find(uid, list_name, item_text)
        if not rows:
            # Search across all lists
            rows = self._fuzzy_find(uid, None, item_text)
            if rows:
                list_name = rows[0]["list_name"]
            else:
                return SkillResult(f"Couldn't find _{item_text}_ in any list.", success=False)

        ids   = [r["id"] for r in rows]
        names = ", ".join(f"*{r['item']}*" for r in rows)
        self._db.table("lists").delete().in_("id", ids).execute()
        return await self._render_list(uid, list_name, prefix=f"✅ Done — {names}")

    async def _confirm_clear(self, uid: int, list_name: str) -> SkillResult:
        count = len(
            self._db.table("lists").select("id")
                .eq("user_id", uid).eq("list_name", list_name).execute().data
        )
        if count == 0:
            return SkillResult(
                f"_{list_name.title()}_ is already empty.",
                reply_markup=self._list_keyboard(list_name),
            )
        return SkillResult(
            f"🗑 Clear all {count} item{'s' if count != 1 else ''} from *{list_name.title()}*?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Yes, clear", callback_data=f"list:clear_confirm:{list_name}"),
                InlineKeyboardButton("❌ Cancel",      callback_data=f"list:show:{list_name}"),
            ]]),
        )

    # ── Rendering ─────────────────────────────────────────────────────────────

    async def _render_all(self, uid: int, prefix: str = "") -> SkillResult:
        rows = self._db.table("lists").select("list_name").eq("user_id", uid).execute().data

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["list_name"]] = counts.get(r["list_name"], 0) + 1

        lines = []
        if prefix:
            lines += [prefix, ""]

        lines.append("📋 *Your Lists*\n")

        if not counts:
            lines.append("_No lists yet — create one!_")
        else:
            for name in sorted(counts):
                n = counts[name]
                lines.append(f"{_emoji(name)} *{name.title()}*  · {n} item{'s' if n != 1 else ''}")

        lines += ["", "_Tap a list to open it_"]
        return SkillResult("\n".join(lines), reply_markup=self._all_keyboard(counts))

    async def _render_list(self, uid: int, list_name: str, prefix: str = "") -> SkillResult:
        rows = (
            self._db.table("lists").select("*")
                .eq("user_id", uid).eq("list_name", list_name)
                .order("created_at").execute().data
        )
        lines = []
        if prefix:
            lines += [prefix, ""]

        lines.append(f"{_emoji(list_name)} *{list_name.title()}*\n")

        if not rows:
            lines.append("_Empty — add something!_")
        else:
            for r in rows:
                qty = f"  {r['quantity']}" if r.get("quantity") else ""
                lines.append(f"• *{r['item']}*{qty}")

        lines.append("")
        n = len(rows)
        if n == 1:
            lines.append("_1 item_")
        elif n > 1:
            lines.append(f"_{n} items_")

        return SkillResult("\n".join(lines), reply_markup=self._list_keyboard(list_name))

    # ── Keyboards ─────────────────────────────────────────────────────────────

    def _all_keyboard(self, counts: dict) -> InlineKeyboardMarkup:
        buttons: list[list] = []
        row: list = []
        for name in sorted(counts):
            row.append(InlineKeyboardButton(
                f"{_emoji(name)} {name.title()}", callback_data=f"list:show:{name}"
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("➕ New list", callback_data="list:new")])
        return InlineKeyboardMarkup(buttons)

    def _list_keyboard(self, list_name: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Add",    callback_data=f"list:add:{list_name}"),
                InlineKeyboardButton("✅ Done",   callback_data=f"list:done:{list_name}"),
                InlineKeyboardButton("🗑 Clear",  callback_data=f"list:clear:{list_name}"),
            ],
            [InlineKeyboardButton("← All lists", callback_data="list:all")],
        ])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fuzzy_find(self, uid: int, list_name: str | None, item_text: str) -> list:
        item_text = item_text.strip("\"'""''")
        q = self._db.table("lists").select("id,item,list_name").eq("user_id", uid)
        if list_name:
            q = q.eq("list_name", list_name)
        return q.ilike("item", f"%{item_text}%").execute().data

    def _parse_action(self, text: str) -> str:
        t = text.lower().strip()
        if not t:
            return "show_all"
        if any(t.startswith(w) for w in ["add ", "buy ", "get ", "need ", "pick up "]):
            return "add"
        if any(t.startswith(w) for w in ["done ", "remove ", "delete ", "tick ", "bought ", "got "]):
            return "done"
        if t.startswith("clear ") or t.startswith("empty "):
            return "clear"
        return "show"

    def _parse_list_name(self, text: str) -> str:
        t = text.lower()
        for name in LIST_EMOJI:
            if name in t:
                return name
        words = t.split()
        return words[-1] if words else "groceries"

    def _parse_multi_items(self, text: str, ext: dict) -> tuple[list[str], str]:
        """Return (items, list_name) when input looks like a multi-item add, else ([], list_name)."""
        t = text.strip()
        has_separately = bool(re.search(r'\bseparately\b', t, re.IGNORECASE))
        has_newlines   = '\n' in t

        if not has_separately and not has_newlines:
            return [], ext.get("list_name") or self._parse_list_name(t)

        # Strip add verb
        t = re.sub(r'^(add|buy|get|need|pick up|put)\s+', '', t, flags=re.IGNORECASE).strip()
        # Strip "(separately):" or "separately:" prefix
        t = re.sub(r'^\(?separately\)?\s*:?\s*', '', t, flags=re.IGNORECASE).strip()

        # Extract list name from "to <list>" at the end of the first line
        list_name = ext.get("list_name") or "groceries"
        first_line = t.split('\n')[0]
        if " to " in first_line.lower():
            idx = first_line.lower().rfind(" to ")
            potential = first_line[idx + 4:].strip().lower()
            if len(potential.split()) <= 2:
                list_name = potential
                t = t[: t.index(first_line[idx:])].strip() if first_line[idx:] in t else t

        # Split items
        if has_newlines:
            items = [ln.strip().lstrip('•-–·').strip() for ln in t.split('\n') if ln.strip()]
        else:
            items = [i.strip() for i in re.split(r'[,;]+', t) if i.strip()]

        return items, list_name

    def _parse_add(self, text: str, ext: dict) -> tuple[str, str, str]:
        """Returns (item, quantity, list_name)."""
        if ext.get("item"):
            return ext.get("item", ""), ext.get("quantity", ""), ext.get("list_name", "groceries")

        t = text.strip()
        t = re.sub(r'^(add|buy|get|need|pick up|put)\s+', '', t, flags=re.IGNORECASE).strip()

        list_name = "groceries"
        if " to " in t.lower():
            idx = t.lower().rfind(" to ")
            list_name = t[idx + 4:].strip().lower()
            t = t[:idx].strip()

        item, qty = self._parse_quantity(t)
        return item, qty, list_name

    def _parse_quantity(self, text: str) -> tuple[str, str]:
        # "2x milk" or "2 x milk"
        m = re.match(r'^(\d+(?:\.\d+)?)\s*[x×]\s+(.+)$', text, re.IGNORECASE)
        if m:
            return m.group(2).strip(), f"×{m.group(1)}"
        # "500g chicken", "1.5kg beef", "200ml milk"
        m = re.match(r'^(\d+(?:\.\d+)?(?:g|kg|ml|l|oz|lb|pcs|pack|bags?))\s+(.+)$', text, re.IGNORECASE)
        if m:
            return m.group(2).strip(), m.group(1)
        # "3 avocados" (plain number + item)
        m = re.match(r'^(\d+)\s+([a-zA-Z].+)$', text)
        if m:
            return m.group(2).strip(), f"×{m.group(1)}"
        # "milk x2" at end
        m = re.match(r'^(.+?)\s+[x×](\d+)$', text, re.IGNORECASE)
        if m:
            return m.group(1).strip(), f"×{m.group(2)}"
        return text.strip(), ""

    def _parse_item_name(self, text: str) -> str:
        t = text
        for prefix in ["done ", "remove ", "delete ", "tick ", "mark ", "bought ", "got "]:
            if t.lower().startswith(prefix):
                t = t[len(prefix):]
        for suffix in [" done", " bought", " got", " complete", " completed"]:
            if t.lower().endswith(suffix):
                t = t[:-len(suffix)]
        return t.strip()


_skill_instance = ListSkill()
registry.register(_skill_instance)
