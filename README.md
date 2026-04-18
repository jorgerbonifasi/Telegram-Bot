# Personal Telegram Bot

A personal bot with pluggable skills. Starts with **Google Calendar** and **Daily To-Do**, built to be extended.

---

## Quick start

### 1. Clone & install
```bash
git clone <your-repo>
cd telegram-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your bot on Telegram
1. Open Telegram → search **@BotFather**
2. Send `/newbot`, follow prompts
3. Copy the token → paste into `.env` as `TELEGRAM_BOT_TOKEN`

### 3. Configure `.env`
```bash
cp .env.example .env
# Edit .env with your keys
```

**Get your Telegram user ID:**
Message **@userinfobot** on Telegram. Paste the ID into `ALLOWED_USER_IDS`.

### 4. Supabase (to-do skill)
1. Create a free project at [supabase.com](https://supabase.com)
2. Run this SQL in the Supabase SQL editor:
```sql
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
```
3. Paste `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` into `.env`

### 5. Google Calendar (gcal skill)
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → enable **Google Calendar API**
3. Credentials → Create OAuth 2.0 Client ID → **Desktop app**
4. Download JSON → save as `client_secret.json` in project root
5. Run the one-time auth setup:
```bash
python -m skills.gcal.auth_setup
```
This opens your browser. Sign in and allow access. A `google_token.pickle` is saved locally.

### 6. Run the bot
```bash
python bot.py
```

Open Telegram on your iPhone, find your bot, and send `/start`.

---

## Usage

### Natural language (just type)
```
meeting with Kate tomorrow at 3pm for 1 hour
add review PRs to work high priority
what are my tasks for today?
dentist appointment Friday 10am
mark review PRs done
```

### Commands
| Command | Skill | Example |
|---------|-------|---------|
| `/todo` or `/t` | To-do | `/todo add buy milk` |
| `/tasks` | To-do | `/tasks list work` |
| `/cal` or `/event` | Google Cal | `/cal team sync Thursday 2pm` |
| `/skills` | — | Shows skill menu |
| `/help` | — | Shows all skills + examples |

---

## Adding a new skill

1. Create `skills/my_skill/__init__.py`:

```python
from core.skill_base import BaseSkill, SkillResult, registry
from telegram import Update
from telegram.ext import ContextTypes

class MySkill(BaseSkill):
    name        = "myskill"
    description = "Does something useful"
    commands    = ["/my", "/ms"]
    examples    = ["do the thing", "/my action"]

    async def handle(self, update, context, user_text, extracted=None) -> SkillResult:
        return SkillResult(f"You said: {user_text}")

registry.register(MySkill())
```

2. Add `"skills.my_skill"` to `SKILL_MODULES` in `bot.py`
3. Restart — the skill appears in `/help` and NLU routing automatically

---

## Project structure

```
telegram-bot/
├── bot.py                    # Main entry point
├── requirements.txt
├── .env.example
├── client_secret.json        # Google OAuth (gitignored)
├── google_token.pickle       # Auto-generated (gitignored)
├── core/
│   ├── skill_base.py         # BaseSkill, SkillResult, SkillRegistry
│   ├── nlu.py                # Claude-powered intent classifier
│   └── auth.py               # User ID allowlist
└── skills/
    ├── todo/
    │   └── __init__.py       # Daily to-do (Supabase)
    └── gcal/
        ├── __init__.py       # Google Calendar event creator
        └── auth_setup.py     # One-time OAuth script
```

---

## Deploying to Railway (24/7)

### 1. Google OAuth — do this locally first
```bash
# On your laptop (not the server):
python -m skills.gcal.auth_setup     # opens browser, saves google_token.pickle
python scripts/encode_token.py       # prints a long base64 string → copy it
```
You'll paste that string into Railway as `GOOGLE_TOKEN_B64` in step 4.

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "init"
gh repo create telegram-bot --private --push   # or use github.com
```

### 3. Create Railway project
1. Go to [railway.app](https://railway.app) → **New Project**
2. **Deploy from GitHub repo** → select your repo
3. Railway detects the `Dockerfile` automatically and starts building

### 4. Set environment variables
In Railway → your service → **Variables**, add each key from `.env.example`:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `SUPABASE_URL` | From Supabase project settings |
| `SUPABASE_SERVICE_KEY` | From Supabase project settings |
| `GOOGLE_TOKEN_B64` | Output of `encode_token.py` |
| `ALLOWED_USER_IDS` | Your Telegram user ID |

### 5. Deploy
Railway redeploys automatically on every `git push`. Check logs in the Railway dashboard — you should see:
```
[bot] Started with skills: ['todo', 'gcal']
[gcal] Google Calendar connected ✓
[bot] Polling...
```

### Token refresh
Google OAuth tokens expire roughly every 6 months. When they do:
```bash
python -m skills.gcal.auth_setup     # re-auth locally
python scripts/encode_token.py       # get new base64 string
# Paste new value into Railway → Variables → GOOGLE_TOKEN_B64
# Railway auto-redeploys
```

---

## Security notes
- `ALLOWED_USER_IDS` ensures only you can use the bot
- Never commit `.env`, `client_secret.json`, or `google_token.pickle`
- Add them to `.gitignore` (already listed below)
