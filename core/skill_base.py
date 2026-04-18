"""
core/skill_base.py  —  Base class + auto-registry for all skills.

To add a new skill:
  1. Create  skills/my_skill/__init__.py
  2. Define a class inheriting BaseSkill
  3. Call registry.register(MySkill()) at module level
  4. Import the module in bot.py's SKILL_MODULES list

That's it — /help, routing, and menus update automatically.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from telegram import Update
from telegram.ext import ContextTypes


@dataclass
class SkillResult:
    """Unified return type from every skill."""
    text: str                               # Markdown text sent to user
    success: bool = True
    parse_mode: str = "Markdown"
    suggestions: list[str] = field(default_factory=list)  # Quick-reply button labels


class BaseSkill(ABC):
    """
    Every skill must define class-level attributes:
      name        — short slug, e.g. "todo"
      description — one-liner shown in /help
      commands    — list of /commands this skill owns, e.g. ["/todo", "/t"]
      examples    — example phrases shown in /help (optional)

    And implement:
      handle(update, context, user_text) -> SkillResult
    """
    name: str = ""
    description: str = ""
    commands: list[str] = []
    examples: list[str] = []

    @abstractmethod
    async def handle(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
    ) -> SkillResult:
        ...

    async def on_load(self) -> None:
        """Override for async initialisation at startup (e.g. ensure DB table exists)."""
        pass


# ── Registry ──────────────────────────────────────────────────────────────────

class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill) -> None:
        self._skills[skill.name] = skill

    def all(self) -> list[BaseSkill]:
        return list(self._skills.values())

    def by_command(self, command: str) -> Optional[BaseSkill]:
        cmd = command.lstrip("/").split("@")[0].lower()
        for skill in self._skills.values():
            if cmd in [c.lstrip("/") for c in skill.commands]:
                return skill
        return None

    def by_name(self, name: str) -> Optional[BaseSkill]:
        return self._skills.get(name)

    async def load_all(self) -> None:
        for skill in self._skills.values():
            await skill.on_load()


registry = SkillRegistry()
