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
    "skills.docs",
    "skills.lists",
    "skills.habits",
    "skills.briefing",   # must be last — borrows gcal service after it loads
]
for mod in SKILL_MODULES:
    importlib.import_module(mod)

from core.skill_base import registry, SkillResult
from core.nlu import classify
from core.auth import require_auth

# Skills that expose callback handlers for inline buttons
from skills.gcal     import _skill_instance as gcal_skill
from skills.todo     import _skill_instance as todo_skill
from skills.lists    import _skill_instance as list_skill
from skills.habits   import _skill_instance as habits_skill
from skills.briefing import _skill_instance as briefing_skill


# ── Scheduled job callbacks ───────────────────────────────────────────────────

async def _job_morning_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    await briefing_skill.send_morning(context.bot)

async def _job_evening_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    await briefing_skill.send_evening(context.bot)

async def _job_habits_lunch_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    await habits_skill.send_reminder(context.bot, "Afternoon")

async def _job_habits_dinner_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    await habits_skill.send_reminder(context.bot, "Evening")


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

    # Conversational state: gcal edit / add flows
    gcal_state = context.user_data.get("gcal_state")
    if gcal_state:
        if user_text.lower().strip() in ("cancel", "nevermind", "never mind", "exit", "stop"):
            context.user_data.pop("gcal_state")
            await update.message.reply_text("Cancelled.")
            return
        context.user_data.pop("gcal_state")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        if isinstance(gcal_state, dict) and gcal_state.get("mode") == "editing":
            result = await gcal_skill.handle_edit(update, context, user_text, gcal_state["key"])
        else:
            result = await gcal_skill.handle(update, context, user_text)
        if result and result.text:
            await _send_result(update, result)
        return

    # Conversational state: lists flows
    list_state = context.user_data.get("list_state")
    if list_state:
        if user_text.lower().strip() in ("cancel", "nevermind", "never mind", "exit", "stop"):
            context.user_data.pop("list_state")
            await update.message.reply_text("Cancelled.")
            return
        context.user_data.pop("list_state")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        mode      = list_state.get("mode")
        list_name = list_state.get("list", "groceries")
        uid       = update.effective_user.id
        if mode == "awaiting_add":
            result = await list_skill._add_item(uid, list_name, *list_skill._parse_quantity(user_text))
        elif mode == "awaiting_done":
            result = await list_skill._remove_item(uid, list_name, user_text)
        elif mode == "awaiting_new":
            result = await list_skill._render_list(uid, user_text.strip().lower())
        else:
            result = await list_skill.handle(update, context, user_text)
        if result.text:
            await _send_result(update, result)
        return

    # Conversational state: todo flows
    todo_state = context.user_data.get("todo_state")
    if todo_state:
        if user_text.lower().strip() in ("cancel", "nevermind", "never mind", "exit", "stop", "nope", "no"):
            context.user_data.pop("todo_state")
            await update.message.reply_text("Cancelled.")
            return
        context.user_data.pop("todo_state")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        if todo_state == "awaiting_add":
            intent = await classify(user_text)
            extracted = intent.get("extracted", {})
            extracted["action"] = "add"
        elif todo_state == "awaiting_done":
            extracted = {"action": "done", "task": user_text}
        else:
            extracted = {}
        result = await todo_skill.handle(update, context, user_text, extracted=extracted)
        if result.text:
            await _send_result(update, result)
        return

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


@require_auth
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route photo messages to gcal for event extraction via Claude vision."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    photo      = update.message.photo[-1]           # highest resolution
    photo_file = await context.bot.get_file(photo.file_id)
    photo_bytes = await photo_file.download_as_bytearray()
    caption    = update.message.caption or ""
    result     = await gcal_skill.handle_photo(update, context, bytes(photo_bytes), caption)
    if result and result.text:
        await _send_result(update, result)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline keyboard callbacks."""
    query = update.callback_query
    data  = query.data

    if data.startswith("gcal_"):
        await gcal_skill.handle_callback(update, context)
        return

    if data.startswith("todo:"):
        await todo_skill.handle_callback(update, context)
        return

    if data.startswith("list:"):
        await list_skill.handle_callback(update, context)
        return

    if data.startswith("habits_"):
        await habits_skill.handle_callback(update, context)
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
    if result.reply_markup is not None:
        keyboard = result.reply_markup
    elif result.suggestions:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(s, callback_data=f"nlu_suggest:{s}")
            for s in result.suggestions[:3]
        ]])
    else:
        keyboard = None
    await update.message.reply_text(
        result.text,
        parse_mode=result.parse_mode,
        reply_markup=keyboard,
    )


# ── Startup ───────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Set bot commands menu + run skill on_load hooks + schedule daily briefings."""
    await registry.load_all()

    commands = [BotCommand("start", "Show all skills"), BotCommand("help", "Help")]
    for skill in registry.all():
        cmd = skill.commands[0].lstrip("/")
        commands.append(BotCommand(cmd, skill.description[:64]))
    await app.bot.set_my_commands(commands)

    # Schedule daily briefings (London time)
    from datetime import time as dtime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    app.job_queue.run_daily(_job_morning_briefing,      time=dtime( 8,  0, tzinfo=london), name="morning_briefing")
    app.job_queue.run_daily(_job_evening_briefing,      time=dtime(20,  0, tzinfo=london), name="evening_briefing")
    app.job_queue.run_daily(_job_habits_lunch_reminder, time=dtime(14,  0, tzinfo=london), name="habits_lunch_reminder")
    app.job_queue.run_daily(_job_habits_dinner_reminder,time=dtime(20, 30, tzinfo=london), name="habits_dinner_reminder")
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

    # Photos → gcal skill (event extraction from screenshots)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Free-text → NLU
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("[bot] Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
