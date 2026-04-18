"""
skills/gcal/auth_setup.py  —  One-time Google OAuth authorisation.

Usage (from project root):
    python -m skills.gcal.auth_setup

This opens your browser, you sign in with your Google account,
and saves google_token.pickle to the project root.
You only need to do this once (tokens auto-refresh thereafter).

Prerequisites:
  1. Google Cloud Console → APIs & Services → Credentials
  2. Create OAuth 2.0 Client ID (Desktop app)
  3. Download JSON → save as client_secret.json in project root
"""

import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES      = ["https://www.googleapis.com/auth/calendar.events"]
SECRET_FILE = Path("client_secret.json")
TOKEN_FILE  = Path("google_token.pickle")


def main():
    if not SECRET_FILE.exists():
        print(f"❌  {SECRET_FILE} not found.")
        print("   Download your OAuth credentials from Google Cloud Console")
        print("   and save them as client_secret.json in the project root.")
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(SECRET_FILE), SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "wb") as f:
        pickle.dump(creds, f)

    print(f"\n✅  Authorised! Token saved to {TOKEN_FILE}")
    print("   Your bot can now create Google Calendar events.")


if __name__ == "__main__":
    main()
