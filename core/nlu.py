"""
core/nlu.py  —  Natural language → skill routing via Claude.

Free-text messages (not starting with /) are classified here.
Claude also extracts structured intent (e.g. event details, task text)
so skills don't need to do their own parsing.
"""

from __future__ import annotations
import json
import anthropic
from core.skill_base import registry

_client = anthropic.Anthropic()


def _system_prompt() -> str:
    skills = registry.all()
    skill_list = "\n".join(
        f'  - "{s.name}": {s.description}  (commands: {", ".join(s.commands)})'
        for s in skills
    )
    return f"""You are a routing assistant for a personal Telegram bot.
Given a user message, you must:
1. Identify which skill should handle it.
2. Extract any structured data needed by that skill.

Available skills:
{skill_list}
  - "unknown": message doesn't match any skill

Respond ONLY with a JSON object, no markdown, no explanation:
{{
  "skill": "<skill_name>",
  "confidence": <0.0-1.0>,
  "extracted": {{
    // skill-specific fields — see below

    // For "gcal":
    //   "title": string,
    //   "date": "YYYY-MM-DD or natural e.g. tomorrow",
    //   "time": "HH:MM or natural e.g. 3pm",
    //   "duration_minutes": int,
    //   "description": string (optional),
    //   "location": string (optional)

    // For "todo":
    //   "action": "add" | "list" | "done" | "delete" | "clear",
    //   "task": string — rewrite as a clean, natural action phrase. Rules: (1) capitalize only the first word and proper nouns, (2) no colons — fold lists into natural language (e.g. "Clean: floors, trash" → "Clean floors and trash"), (3) fix typos and spacing, (4) expand abbreviations (e.g. "pickup" → "Pick up", "prs" → "PRs"), (5) keep it concise. Examples: "wash dishes" → "Wash dishes", "buy tickets: frankfurt birthday" → "Buy tickets for Frankfurt and birthday".
    //   "section": "work" | "personal" | null,
    //   "priority": "high" | "medium" | "low" | null
  }},
  "reply_if_unknown": "A short helpful message if skill is unknown"
}}"""


async def classify(user_text: str) -> dict:
    """
    Returns dict with keys: skill, confidence, extracted, reply_if_unknown.
    Falls back to {"skill": "unknown"} on any error.
    """
    try:
        response = _client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system=_system_prompt(),
            messages=[{"role": "user", "content": user_text}],
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        print(f"[NLU] classify error: {e}")
        return {"skill": "unknown", "confidence": 0.0, "extracted": {}}
