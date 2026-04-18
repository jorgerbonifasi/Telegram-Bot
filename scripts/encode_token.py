"""
scripts/encode_token.py  —  Run this LOCALLY after auth_setup.

Encodes google_token.pickle as base64 and prints the value to paste
into Railway as the GOOGLE_TOKEN_B64 environment variable.

Usage:
    python scripts/encode_token.py
"""

import base64, pickle, sys
from pathlib import Path

TOKEN_FILE = Path("google_token.pickle")

if not TOKEN_FILE.exists():
    print("❌  google_token.pickle not found.")
    print("    Run:  python -m skills.gcal.auth_setup  first.")
    sys.exit(1)

data = TOKEN_FILE.read_bytes()
encoded = base64.b64encode(data).decode("utf-8")

print("\n✅  Copy the value below and set it as GOOGLE_TOKEN_B64 in Railway:\n")
print(encoded)
print("\nDone. This variable replaces the pickle file on the server.")
