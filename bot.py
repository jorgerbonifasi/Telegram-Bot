"""
bot.py  —  Main entry point for the Telegram bot.

Start:
    python bot.py

Add a new skill:
    1. Create skills/my_skill/__init__.py  (inherit BaseSkill, call registry.register)
    2. Add "skills.my_skill" to SKILL_MODULES below
    3. Restart the bot — /help updates automatically
"""

import os
import importlib
from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

load_dotenv()

# ── Import all skill modules (order = /help order) ───────────────────────────
SKILL_MODULES = [
    "skills.todo",
    "skills.gcal",
    # "skills.my_new_skill",   ← add new skills here
]
for mod in SKILL_MODULES:
    importlib.import_module(mod)

from core.skill_base import registry, SkillResult
from core.nlu import classify
from core.auth import require_auth

# GCal skill exposes a callback handler for confirm/cancel buttons
from skills.gcal import _skill_instance as gcal_skill


# ── Handlers ──────────────────────────────────────────────────────────────────

@require_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    skills = registry.all()
    lines = ["👋 *Your personal bot is running.*\n"]
    lines.append("*Skills:*")
    for s in skills:
        cmds = " · ".join(s.commands)
        lines.append(f"  • *{s.name}* — {s.description}")
        lines.append(f"    Commands: `{cmds}`")
        if s.examples:
            lines.append(f"    e.g. _{s.examples[0]}_")
    lines.append("\nOr just type naturally — I'll figure out which skill to use.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


@require_auth
async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show skill menu as inline buttons."""
    skills = registry.all()
    buttons = [[InlineKeyboardButton(
        f"{s.name} — {s.description}", callback_data=f"skill_menu:{s.name}"
    )] for s in skills]
    await update.message.reply_text(
        "Choose a skill:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@require_auth
async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route /commands to their owning skill."""
    command = update.message.text.split()[0]
    skill = registry.by_command(command)
    if not skill:
        await update.message.reply_text(f"Unknown command: `{command}`\nTry /help", parse_mode="Markdown")
        return

    # Strip the command prefix from the text before passing to skill
    user_text = update.message.text[len(command):].strip()
    result = await skill.handle(update, context, user_text)
    if result.text:
        await _send_result(update, result)


@require_auth
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route free-text messages via NLU."""
    user_text = update.message.text

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    intent = await classify(user_text)
    skill_name = intent.get("skill", "unknown")
    skill = registry.by_name(skill_name)

    if not skill or skill_name == "unknown":
        reply = intent.get("reply_if_unknown") or (
            "I'm not sure what you mean. Try /help to see what I can do."
        )
        await update.message.reply_text(reply)
        return

    extracted = intent.get("extracted", {})
    result = await skill.handle(update, context, user_text, extracted=extracted)
    if result.text:
        await _send_result(update, result)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline keyboard callbacks."""
    query = update.callback_query
    data  = query.data

    if data.startswith("gcal_"):
        await gcal_skill.handle_callback(update, context)
        return

    if data.startswith("skill_menu:"):
        skill_name = data.split(":", 1)[1]
        skill = registry.by_name(skill_name)
        if skill:
            lines = [
                f"*{skill.name}* — {skill.description}",
                f"Commands: `{'  '.join(skill.commands)}`",
                "",
                "*Examples:*",
            ] + [f"  _{e}_" for e in skill.examples]
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
        return

    await query.answer()


# ── Utilities ────────────────────────────────────────────────────────────────

async def _send_result(update: Update, result: SkillResult) -> None:
    keyboard = None
    if result.suggestions:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(s, callback_data=f"nlu_suggest:{s}")
            for s in result.suggestions[:3]
        ]])
    await update.message.reply_text(
        result.text,
        parse_mode=result.parse_mode,
        reply_markup=keyboard,
    )


# ── Startup ───────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Set bot commands menu + run skill on_load hooks."""
    await registry.load_all()

    commands = [BotCommand("start", "Show all skills"), BotCommand("help", "Help")]
    for skill in registry.all():
        for cmd in skill.commands:
            commands.append(BotCommand(cmd.lstrip("/"), skill.description[:64]))
    await app.bot.set_my_commands(commands)
    print(f"[bot] Started with skills: {[s.name for s in registry.all()]}")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # Command handlers — catch all known skill commands + built-ins
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("skills", cmd_skills))

    # Route all other /commands to skill dispatcher
    app.add_handler(MessageHandler(filters.COMMAND, handle_command))

    # Free-text → NLU
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("[bot] Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
