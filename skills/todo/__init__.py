"""
skills/todo/__init__.py  —  Daily To-Do Manager skill.

Supabase table: todos
  id          uuid primary key default gen_random_uuid()
  user_id     bigint not null          -- Telegram user ID
  text        text not null
  section     text default 'personal'  -- 'work' | 'personal'
  priority    text default 'medium'    -- 'high' | 'medium' | 'low'
  status      text default 'todo'      -- 'todo' | 'in_progress' | 'done'
  created_at  timestamptz default now()
  date        date default current_date

Run this in Supabase SQL editor to create the table:
  create table todos (
    id         uuid primary key default gen_random_uuid(),
    user_id    bigint not null,
    text       text not null,
    section    text default 'personal',
    priority   text default 'medium',
    status     text default 'todo',
    created_at timestamptz default now(),
    date       date default current_date
  );
  create index on todos(user_id, date, status);
"""

from __future__ import annotations
import os
from datetime import date
from supabase import create_client, Client
from telegram import Update
from telegram.ext import ContextTypes
from core.skill_base import BaseSkill, SkillResult, registry

PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
STATUS_EMOJI   = {"todo": "⬜", "in_progress": "🔄", "done": "✅"}


class TodoSkill(BaseSkill):
    name        = "todo"
    description = "Manage your daily to-do list with Work and Personal sections"
    commands    = ["/todo", "/t", "/tasks"]
    examples    = [
        "add buy groceries to personal",
        "add review PRs to work high priority",
        "list",
        "list work",
        "mark buy groceries done",
        "delete buy groceries",
        "/todo list",
    ]

    def __init__(self) -> None:
        self._db: Client | None = None

    async def on_load(self) -> None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if url and key:
            self._db = create_client(url, key)
        else:
            print("[todo] WARNING: Supabase credentials not set — todo skill disabled")

    # ── main entry point ──────────────────────────────────────────────────────

    async def handle(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
        extracted: dict | None = None,
    ) -> SkillResult:
        if not self._db:
            return SkillResult("⚠️ Todo skill not configured (missing Supabase credentials).", success=False)

        uid   = update.effective_user.id
        ext   = extracted or {}
        action = ext.get("action") or self._parse_action(user_text)

        if action == "add":
            return await self._add(uid, ext, user_text)
        elif action == "list":
            return await self._list(uid, ext.get("section"))
        elif action == "done":
            return await self._set_status(uid, ext.get("task", ""), "done")
        elif action == "in_progress":
            return await self._set_status(uid, ext.get("task", ""), "in_progress")
        elif action == "delete":
            return await self._delete(uid, ext.get("task", ""))
        elif action == "clear":
            return await self._clear_done(uid)
        else:
            return SkillResult(
                "I didn't understand that todo action.\n\n"
                "Try: _add_, _list_, _list work_, _mark X done_, _delete X_, _clear done_",
                suggestions=["list", "list work", "clear done"],
            )

    # ── actions ───────────────────────────────────────────────────────────────

    async def _add(self, uid: int, ext: dict, raw: str) -> SkillResult:
        text     = ext.get("task") or self._strip_command(raw)
        section  = ext.get("section") or ("work" if "work" in raw.lower() else "personal")
        priority = ext.get("priority") or "medium"

        if not text:
            return SkillResult("What task do you want to add?", success=False)

        self._db.table("todos").insert({
            "user_id":  uid,
            "text":     text,
            "section":  section,
            "priority": priority,
            "status":   "todo",
            "date":     str(date.today()),
        }).execute()

        emoji = PRIORITY_EMOJI[priority]
        return SkillResult(
            f"✅ Added to *{section}*: {text} {emoji}",
            suggestions=["list", f"list {section}"],
        )

    async def _list(self, uid: int, section: str | None) -> SkillResult:
        q = self._db.table("todos").select("*") \
            .eq("user_id", uid) \
            .eq("date", str(date.today())) \
            .neq("status", "done") \
            .order("priority")

        if section in ("work", "personal"):
            q = q.eq("section", section)

        rows = q.execute().data

        if not rows:
            label = f" ({section})" if section else ""
            return SkillResult(
                f"No open tasks{label} for today 🎉",
                suggestions=["add a task"],
            )

        # Group by section
        grouped: dict[str, list] = {"work": [], "personal": []}
        for r in rows:
            grouped.setdefault(r["section"], []).append(r)

        lines = []
        for sec, tasks in grouped.items():
            if not tasks:
                continue
            lines.append(f"*{sec.upper()}*")
            for t in tasks:
                p = PRIORITY_EMOJI[t.get("priority", "medium")]
                s = STATUS_EMOJI[t.get("status", "todo")]
                lines.append(f"  {s} {p} {t['text']}")
            lines.append("")

        return SkillResult("\n".join(lines).strip(), suggestions=["clear done", "list work"])

    async def _set_status(self, uid: int, task_text: str, status: str) -> SkillResult:
        if not task_text:
            return SkillResult("Which task? e.g. _mark buy groceries done_", success=False)

        # Fuzzy match: find tasks where text contains the search term
        rows = self._db.table("todos").select("id,text") \
            .eq("user_id", uid) \
            .eq("date", str(date.today())) \
            .ilike("text", f"%{task_text}%") \
            .execute().data

        if not rows:
            return SkillResult(f"Couldn't find a task matching _{task_text}_", success=False)

        ids = [r["id"] for r in rows]
        self._db.table("todos").update({"status": status}).in_("id", ids).execute()

        emoji = STATUS_EMOJI[status]
        names = ", ".join(r["text"] for r in rows)
        return SkillResult(f"{emoji} Marked *{status}*: {names}", suggestions=["list"])

    async def _delete(self, uid: int, task_text: str) -> SkillResult:
        if not task_text:
            return SkillResult("Which task should I delete?", success=False)

        rows = self._db.table("todos").select("id,text") \
            .eq("user_id", uid) \
            .ilike("text", f"%{task_text}%") \
            .execute().data

        if not rows:
            return SkillResult(f"No task found matching _{task_text}_", success=False)

        ids = [r["id"] for r in rows]
        self._db.table("todos").delete().in_("id", ids).execute()
        names = ", ".join(r["text"] for r in rows)
        return SkillResult(f"🗑️ Deleted: {names}", suggestions=["list"])

    async def _clear_done(self, uid: int) -> SkillResult:
        self._db.table("todos").delete() \
            .eq("user_id", uid) \
            .eq("status", "done") \
            .execute()
        return SkillResult("🧹 Cleared all done tasks.", suggestions=["list"])

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_action(self, text: str) -> str:
        t = text.lower()
        if any(w in t for w in ["add", "new", "create", "remind"]):
            return "add"
        if any(w in t for w in ["list", "show", "what", "tasks", "todo"]):
            return "list"
        if any(w in t for w in ["done", "complete", "finish", "mark"]):
            return "done"
        if any(w in t for w in ["delete", "remove"]):
            return "delete"
        if "clear" in t:
            return "clear"
        return "unknown"

    def _strip_command(self, text: str) -> str:
        words = text.split()
        if words and words[0].startswith("/"):
            words = words[1:]
        # Remove leading action words
        skip = {"add", "new", "create", "task", "todo", "to", "work", "personal"}
        while words and words[0].lower() in skip:
            words = words[1:]
        return " ".join(words)


# Register
registry.register(TodoSkill())
