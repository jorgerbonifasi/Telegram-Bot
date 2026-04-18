"""
skills/todo/__init__.py  —  Daily To-Do Manager

Follows the daily-todo-manager SKILL.md spec exactly:
- Two sections: Work and Personal
- Columns: Task, Status, Lift, Priority, (optional Deadline)
- Sorting: In Progress → Pending (by P0→P1→P2→—, then Small→Medium→Hard→—) → Waiting
- Done tasks are removed entirely
- Always renders full list after every update

Supabase table: todos
  create table todos (
    id         uuid primary key default gen_random_uuid(),
    user_id    bigint not null,
    text       text not null,
    section    text default 'personal',
    status     text default 'Pending',
    lift       text default '—',
    priority   text default '—',
    deadline   text default '—',
    created_at timestamptz default now(),
    date       date default current_date
  );
  create index on todos(user_id, status);
"""

from __future__ import annotations
import os
from supabase import create_client, Client
from telegram import Update
from telegram.ext import ContextTypes
from core.skill_base import BaseSkill, SkillResult, registry

STATUS_ORDER = {"In Progress": 0, "Pending": 1, "In Progress — Waiting": 2}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "—": 3}
LIFT_ORDER = {"Small": 0, "Medium": 1, "Hard": 2, "—": 3}


class TodoSkill(BaseSkill):
    name        = "todo"
    description = "Daily to-do list with Work and Personal sections"
    commands    = ["/todo", "/t", "/tasks"]
    examples    = [
        "add review PRs to work P1",
        "add buy groceries",
        "list",
        "mark review PRs done",
        "review PRs — in progress",
        "move groceries to personal",
    ]

    def __init__(self):
        self._db: Client | None = None
        self._show_deadline = False

    async def on_load(self):
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if url and key:
            self._db = create_client(url, key)
            # Ensure table has deadline column (best-effort)
        else:
            print("[todo] WARNING: Supabase not configured")

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                     user_text: str, extracted: dict | None = None) -> SkillResult:
        if not self._db:
            return SkillResult("⚠️ Todo skill not configured (missing Supabase credentials).", success=False)

        uid = update.effective_user.id
        ext = extracted or {}
        action = ext.get("action") or self._parse_action(user_text)

        if action == "add":
            return await self._add(uid, ext, user_text)
        elif action == "list":
            return await self._render(uid)
        elif action == "done":
            return await self._mark_done(uid, ext.get("task", "") or self._extract_task_name(user_text))
        elif action == "status":
            new_status = ext.get("status") or self._parse_status(user_text)
            task = ext.get("task", "") or self._extract_task_name(user_text)
            return await self._update_field(uid, task, "status", new_status)
        elif action == "move":
            section = "work" if "work" in user_text.lower() else "personal"
            task = ext.get("task", "") or self._extract_task_name(user_text)
            return await self._update_field(uid, task, "section", section)
        elif action == "clear":
            return await self._clear(uid, user_text)
        elif action == "update":
            return await self._update_fields(uid, ext, user_text)
        else:
            return SkillResult(
                "I didn't catch that. Try: _add X_, _list_, _mark X done_, _X — in progress_",
                suggestions=["list", "add task"],
            )

    # ── Actions ───────────────────────────────────────────────────────────────

    async def _add(self, uid: int, ext: dict, raw: str) -> SkillResult:
        text = ext.get("task") or self._strip_add_prefix(raw)
        if not text:
            return SkillResult("What task do you want to add?", success=False)

        section  = ext.get("section") or self._infer_section(raw)
        priority = self._normalize_priority(ext.get("priority", "—"))
        lift     = self._normalize_lift(ext.get("lift", "—"))
        deadline = ext.get("deadline", "—")

        if deadline and deadline != "—":
            self._show_deadline = True

        self._db.table("todos").insert({
            "user_id":  uid,
            "text":     text,
            "section":  section,
            "status":   "Pending",
            "lift":     lift,
            "priority": priority,
            "deadline": deadline,
        }).execute()

        assumption = f" _(added to {section} — move me if wrong)_" if section == self._infer_section(raw) else ""
        return await self._render(uid, prefix=f"Added 👍 *{text}*{assumption}")

    async def _mark_done(self, uid: int, task_text: str) -> SkillResult:
        if not task_text:
            return SkillResult("Which task is done?", success=False)

        rows = self._fuzzy_find(uid, task_text)
        if not rows:
            return SkillResult(f"Couldn't find a task matching _{task_text}_", success=False)

        ids = [r["id"] for r in rows]
        self._db.table("todos").delete().in_("id", ids).execute()
        names = ", ".join(f"*{r['text']}*" for r in rows)
        return await self._render(uid, prefix=f"Done ✅ {names}")

    async def _update_field(self, uid: int, task_text: str, field: str, value: str) -> SkillResult:
        rows = self._fuzzy_find(uid, task_text)
        if not rows:
            return SkillResult(f"Couldn't find _{task_text}_", success=False)
        ids = [r["id"] for r in rows]
        self._db.table("todos").update({field: value}).in_("id", ids).execute()
        names = ", ".join(f"*{r['text']}*" for r in rows)
        return await self._render(uid, prefix=f"Updated 👍 {names}")

    async def _update_fields(self, uid: int, ext: dict, raw: str) -> SkillResult:
        task_text = ext.get("task", "") or self._extract_task_name(raw)
        rows = self._fuzzy_find(uid, task_text)
        if not rows:
            return SkillResult(f"Couldn't find _{task_text}_", success=False)
        updates = {}
        if ext.get("priority"): updates["priority"] = self._normalize_priority(ext["priority"])
        if ext.get("lift"):     updates["lift"]     = self._normalize_lift(ext["lift"])
        if ext.get("deadline"): updates["deadline"] = ext["deadline"]; self._show_deadline = True
        if ext.get("status"):   updates["status"]   = ext["status"]
        if updates:
            ids = [r["id"] for r in rows]
            self._db.table("todos").update(updates).in_("id", ids).execute()
        names = ", ".join(f"*{r['text']}*" for r in rows)
        return await self._render(uid, prefix=f"Updated 👍 {names}")

    async def _clear(self, uid: int, raw: str) -> SkillResult:
        t = raw.lower()
        if "work" in t:
            self._db.table("todos").delete().eq("user_id", uid).eq("section", "work").execute()
            label = "Work"
        elif "personal" in t:
            self._db.table("todos").delete().eq("user_id", uid).eq("section", "personal").execute()
            label = "Personal"
        else:
            self._db.table("todos").delete().eq("user_id", uid).execute()
            label = "all"
        return await self._render(uid, prefix=f"Done ✅ — {label} tasks cleared")

    # ── Rendering ─────────────────────────────────────────────────────────────

    async def _render(self, uid: int, prefix: str = "") -> SkillResult:
        rows = self._db.table("todos").select("*").eq("user_id", uid).execute().data
        work     = [r for r in rows if r.get("section") == "work"]
        personal = [r for r in rows if r.get("section") != "work"]

        lines = []
        if prefix:
            lines.append(prefix)
            lines.append("")

        for section_name, tasks in [("Work", work), ("Personal", personal)]:
            lines.append(f"*{section_name}*")
            if not tasks:
                lines.append("_(no tasks)_")
            else:
                sorted_tasks = sorted(tasks, key=lambda r: (
                    STATUS_ORDER.get(r.get("status", "Pending"), 1),
                    PRIORITY_ORDER.get(r.get("priority", "—"), 3),
                    LIFT_ORDER.get(r.get("lift", "—"), 3),
                ))
                header = "| Task | Status | Lift | Priority |"
                sep    = "|------|--------|------|----------|"
                if self._show_deadline:
                    header = "| Task | Status | Lift | Priority | Deadline |"
                    sep    = "|------|--------|------|----------|----------|"
                lines.append(header)
                lines.append(sep)
                for t in sorted_tasks:
                    row = f"| {t['text']} | {t.get('status','Pending')} | {t.get('lift','—')} | {t.get('priority','—')} |"
                    if self._show_deadline:
                        row += f" {t.get('deadline','—')} |"
                    lines.append(row)
            lines.append("")

        lines.append("What's next?")
        return SkillResult("\n".join(lines), suggestions=["list", "clear all"])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fuzzy_find(self, uid: int, task_text: str) -> list:
        rows = self._db.table("todos").select("id,text").eq("user_id", uid) \
            .ilike("text", f"%{task_text}%").execute().data
        return rows

    def _parse_action(self, text: str) -> str:
        t = text.lower()
        if any(w in t for w in ["add ", "new ", "create "]):       return "add"
        if any(w in t for w in ["list", "show", "tasks", "/todo", "/t", "/tasks"]): return "list"
        if any(w in t for w in ["done", "complete", "finished"]):  return "done"
        if "— in progress" in t or "- in progress" in t:           return "status"
        if "— waiting" in t or "- waiting" in t:                   return "status"
        if "— pending" in t or "- pending" in t:                   return "status"
        if any(w in t for w in ["move ", "this is work", "this is personal"]): return "move"
        if any(w in t for w in ["clear", "remove all"]):           return "clear"
        if any(w in t for w in [", p0", ", p1", ", p2", "small", "medium", "hard", "big", "deadline"]): return "update"
        return "unknown"

    def _parse_status(self, text: str) -> str:
        t = text.lower()
        if "waiting" in t: return "In Progress — Waiting"
        if "in progress" in t: return "In Progress"
        if "pending" in t: return "Pending"
        return "In Progress"

    def _extract_task_name(self, text: str) -> str:
        t = text
        for prefix in ["mark ", "complete ", "done ", "finish "]:
            if t.lower().startswith(prefix):
                t = t[len(prefix):]
        for suffix in [" done", " complete", " finished", " — in progress",
                       " - in progress", " — waiting", " - waiting", " — pending"]:
            if t.lower().endswith(suffix):
                t = t[:-len(suffix)]
        return t.strip()

    def _strip_add_prefix(self, text: str) -> str:
        words = text.split()
        skip = {"add", "new", "create", "task", "todo", "to", "/todo", "/t", "/tasks"}
        while words and words[0].lower() in skip:
            words = words[1:]
        # Strip trailing section/priority hints
        tail_skip = {"work", "personal", "p0", "p1", "p2", "small", "medium", "hard", "big"}
        while words and words[-1].lower() in tail_skip:
            words = words[:-1]
        return " ".join(words)

    def _infer_section(self, text: str) -> str:
        t = text.lower()
        work_signals = ["work", "pr", "review", "meeting", "octopus", "hubspot",
                        "sla", "kate", "jess", "imogen", "hamza", "yesha"]
        if any(w in t for w in work_signals): return "work"
        return "personal"

    def _normalize_priority(self, p: str) -> str:
        p = p.upper().strip()
        if p in ("P0", "P1", "P2"): return p
        return "—"

    def _normalize_lift(self, l: str) -> str:
        l = l.strip().title()
        if l == "Big": return "Hard"
        if l in ("Small", "Medium", "Hard"): return l
        return "—"


registry.register(TodoSkill())
