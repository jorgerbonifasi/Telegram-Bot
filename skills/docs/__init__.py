"""
skills/docs/__init__.py  —  Personal Documents from Google Drive

Searches the user's Drive folder tree and sends matching files directly
as Telegram document attachments.

Required env vars:
  GOOGLE_SERVICE_ACCOUNT_JSON  — full contents of the service account key JSON
  DOCS_FOLDER_ID               — Google Drive folder ID to search within
"""

from __future__ import annotations
import os
import io
import json
import anthropic
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from telegram import Update
from telegram.ext import ContextTypes
from core.skill_base import BaseSkill, SkillResult, registry

_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_claude = anthropic.Anthropic()


class DocsSkill(BaseSkill):
    name        = "docs"
    description = "Fetch and send your personal documents"
    commands    = ["/docs", "/doc"]
    examples    = [
        "send my Spanish passport",
        "show my visa",
        "give me something from medical",
        "my national ID",
    ]

    def __init__(self):
        self._service = None
        self._root_folder_id: str | None = None
        self._index: list[dict] | None = None  # cached file list

    async def on_load(self):
        sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        self._root_folder_id = os.getenv("DOCS_FOLDER_ID")
        if not sa_json or not self._root_folder_id:
            print("[docs] WARNING: missing GOOGLE_SERVICE_ACCOUNT_JSON or DOCS_FOLDER_ID")
            return
        try:
            info = json.loads(sa_json)
            creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
            print("[docs] Google Drive connected")
        except Exception as e:
            print(f"[docs] Failed to init Drive client: {e}")

    # ── Public handler ────────────────────────────────────────────────────────

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                     user_text: str, extracted: dict | None = None) -> SkillResult:
        if not self._service:
            return SkillResult("⚠️ Docs skill not configured (missing Drive credentials).", success=False)

        query = user_text.strip()
        if not query or query.lower() in ("/docs", "/doc"):
            return await self._send_index()

        file_meta = await self._find_file(query)
        if not file_meta:
            return SkillResult(
                f"Couldn't find a document matching _\"{query}\"_.\n\nTry /docs to see all available files.",
                success=False,
            )

        return await self._send_file(update, file_meta)

    # ── Core logic ────────────────────────────────────────────────────────────

    async def _send_index(self) -> SkillResult:
        files = self._list_all_files()
        if not files:
            return SkillResult("No documents found in your Drive folder.", success=False)

        PINNED_FOLDERS = {
            "Identity/National-IDs",
            "Identity/Passports",
            "Identity/Profile-Photos",
            "Legal/UK-Immigration-Codes",
        }

        lines = ["📁 Your documents:", ""]
        current_folder = None
        for f in sorted(files, key=lambda x: (x["folder_path"], x["name"])):
            if f["folder_path"] not in PINNED_FOLDERS:
                continue
            if f["folder_path"] != current_folder:
                current_folder = f["folder_path"]
                lines.append(f"📂 {current_folder}")
            ext = f["name"].rsplit(".", 1)[-1].upper() if "." in f["name"] else ""
            tag = f" [{ext}]" if ext else ""
            lines.append(f"  · {f['name']}{tag}")
        lines.append("")
        lines.append("Ask me for any document by name — I can find anything in your Drive.")
        return SkillResult("\n".join(lines), parse_mode=None)

    async def _find_file(self, query: str) -> dict | None:
        files = self._list_all_files()
        if not files:
            return None

        # Build a simple catalogue string for Claude to reason over
        catalogue = "\n".join(
            f"{i}: {f['folder_path']}/{f['name']}"
            for i, f in enumerate(files)
        )

        response = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system=(
                "You are a file-matching assistant. Given a user query and a numbered list of files, "
                "respond with ONLY the index number of the best matching file. "
                "Prefer PDFs over Google Docs when both exist for the same document. "
                "If nothing matches, respond with -1."
            ),
            messages=[{"role": "user", "content": f"Query: {query}\n\nFiles:\n{catalogue}"}],
        )

        try:
            idx = int(response.content[0].text.strip())
        except ValueError:
            return None

        if idx < 0 or idx >= len(files):
            return None
        return files[idx]

    async def _send_file(self, update: Update, file_meta: dict) -> SkillResult:
        mime = file_meta.get("mimeType", "")
        file_id = file_meta["id"]
        name = file_meta["name"]

        try:
            # Google Docs/Sheets → export as PDF
            if mime == "application/vnd.google-apps.document":
                request = self._service.files().export_media(
                    fileId=file_id, mimeType="application/pdf"
                )
                name = name + ".pdf"
            else:
                request = self._service.files().get_media(fileId=file_id)

            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buf.seek(0)

            await update.message.reply_document(
                document=buf,
                filename=name,
                caption=f"📄 *{name}*\n_📂 {file_meta['folder_path']}_",
                parse_mode="Markdown",
            )
            return SkillResult(f"📤 Sent *{name}*")
        except Exception as e:
            print(f"[docs] Download error: {e}")
            return SkillResult(f"⚠️ Couldn't download _{name}_. Try again.", success=False)

    # ── Drive helpers ─────────────────────────────────────────────────────────

    def _list_all_files(self) -> list[dict]:
        """Walk the folder tree and return a flat list of file metadata."""
        results = []
        self._walk_folder(self._root_folder_id, "", results)
        return results

    def _walk_folder(self, folder_id: str, path: str, results: list):
        try:
            resp = self._service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id, name, mimeType)",
                pageSize=100,
            ).execute()
        except Exception as e:
            print(f"[docs] Drive list error: {e}")
            return

        for item in resp.get("files", []):
            if item["mimeType"] == "application/vnd.google-apps.folder":
                sub_path = f"{path}/{item['name']}" if path else item["name"]
                self._walk_folder(item["id"], sub_path, results)
            else:
                item["folder_path"] = path or "Root"
                results.append(item)


_skill_instance = DocsSkill()
registry.register(_skill_instance)
