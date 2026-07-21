#!/usr/bin/env python3
"""Receive tg-cl.tar.gz and config.json via a temporary Telegram delivery bot.

The bot token is used ONLY to collect these two files. After a successful
download the process exits and the token must not be reused for runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional


API_BASE = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_OVERALL_TIMEOUT = 30 * 60  # 30 minutes to deliver both files
USER_AGENT = "tg-cloner-pro-action/1.0"


class TelegramAPIError(RuntimeError):
    """Raised when the Bot API returns an error payload."""


class DeliveryBot:
    """Minimal Bot API client used solely for file intake."""

    def __init__(self, token: str, allowed_user_ids: set[int]) -> None:
        token = token.strip()
        if not token or ":" not in token:
            raise ValueError("Invalid Telegram bot token format")
        self.token = token
        self.allowed_user_ids = allowed_user_ids
        self.base = f"{API_BASE}/bot{token}"
        self.file_base = f"{API_BASE}/file/bot{token}"
        self.bot_username = "unknown"
        self.offset = 0

    # ------------------------------------------------------------------ API
    def _request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: int = 60,
    ) -> Any:
        url = f"{self.base}/{method}"
        data = None
        headers = {"User-Agent": USER_AGENT}
        if params is not None:
            data = urllib.parse.urlencode(params).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TelegramAPIError(f"HTTP {exc.code} on {method}: {body}") from exc
        except urllib.error.URLError as exc:
            raise TelegramAPIError(f"Network error on {method}: {exc}") from exc

        if not payload.get("ok"):
            desc = payload.get("description") or payload
            raise TelegramAPIError(f"{method} failed: {desc}")
        return payload.get("result")

    def bootstrap(self) -> None:
        me = self._request("getMe")
        self.bot_username = me.get("username") or str(me.get("id", "unknown"))
        # Drop any webhook so long-polling works cleanly.
        self._request("deleteWebhook", {"drop_pending_updates": True})
        # Skip backlog from previous runs.
        self.offset = 0
        updates = self._request(
            "getUpdates",
            {"timeout": 0, "offset": -1, "limit": 1},
            timeout=15,
        )
        if updates:
            self.offset = int(updates[-1]["update_id"]) + 1
        log(f"Delivery bot online: @{self.bot_username}")

    def get_updates(self, timeout: int = DEFAULT_POLL_TIMEOUT) -> list[dict[str, Any]]:
        result = self._request(
            "getUpdates",
            {
                "timeout": timeout,
                "offset": self.offset,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=timeout + 15,
        )
        return result or []

    def send_message(self, chat_id: int, text: str) -> None:
        self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )

    def download_file(self, file_id: str, dest: Path) -> int:
        meta = self._request("getFile", {"file_id": file_id}, timeout=60)
        file_path = meta.get("file_path")
        if not file_path:
            raise TelegramAPIError("getFile returned no file_path")

        url = f"{self.file_base}/{file_path}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as out:
            total = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
        return total

    def advance_offset(self, updates: Iterable[dict[str, Any]]) -> None:
        for update in updates:
            uid = int(update["update_id"]) + 1
            if uid > self.offset:
                self.offset = uid


# ----------------------------------------------------------------- helpers
def log(message: str) -> None:
    print(f"[receive] {message}", flush=True)


def parse_allowed_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids


def is_tar_gz_document(document: dict[str, Any]) -> bool:
    name = (document.get("file_name") or "").lower()
    mime = (document.get("mime_type") or "").lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return True
    if "tg-cl" in name and name.endswith(".gz"):
        return True
    if mime in {"application/gzip", "application/x-gzip", "application/x-tar"}:
        return name.endswith(".gz") or name.endswith(".tar") or not name
    return False


def is_config_document(document: dict[str, Any]) -> bool:
    name = (document.get("file_name") or "").lower()
    mime = (document.get("mime_type") or "").lower()
    if name == "config.json" or name.endswith("/config.json"):
        return True
    if name.endswith(".json") and "config" in name:
        return True
    if mime in {"application/json", "text/plain", "application/octet-stream"}:
        return name.endswith(".json")
    return False


def validate_config_json(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"config.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("config.json must be a JSON object")
    required = ("api_id", "api_hash", "workers")
    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError(f"config.json missing required keys: {', '.join(missing)}")
    workers = data.get("workers")
    if not isinstance(workers, list) or not workers:
        raise RuntimeError("config.json workers must be a non-empty list")


def format_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num} B"


def receive_files(
    bot: DeliveryBot,
    out_dir: Path,
    overall_timeout: int,
) -> tuple[Path, Path]:
    archive_path = out_dir / "tg-cl.tar.gz"
    config_path = out_dir / "config.json"
    stage = "archive"  # archive -> config -> done
    started = time.monotonic()
    instructed_chats: set[int] = set()
    active_chat_id: Optional[int] = None

    welcome = (
        "TG Clone Pro — GitHub Action delivery\n\n"
        "Send the package in this order:\n"
        "  1) tg-cl.tar.gz  (document)\n"
        "  2) config.json   (document)\n\n"
        "This bot is only used to receive those files."
    )

    log("Waiting for user to send tg-cl.tar.gz …")
    log(f"Open Telegram and message @{bot.bot_username}")

    while True:
        elapsed = time.monotonic() - started
        if elapsed > overall_timeout:
            raise TimeoutError(
                f"Timed out after {int(elapsed)}s waiting for delivery "
                f"(stage={stage}). Send files to @{bot.bot_username}."
            )

        try:
            updates = bot.get_updates(timeout=DEFAULT_POLL_TIMEOUT)
        except TelegramAPIError as exc:
            log(f"getUpdates error (will retry): {exc}")
            time.sleep(3)
            continue

        if not updates:
            continue

        bot.advance_offset(updates)

        for update in updates:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            user = message.get("from") or {}
            user_id = user.get("id")
            if chat_id is None or user_id is None:
                continue

            if bot.allowed_user_ids and int(user_id) not in bot.allowed_user_ids:
                log(f"Ignoring unauthorized user_id={user_id}")
                continue

            if active_chat_id is None:
                active_chat_id = int(chat_id)
            elif int(chat_id) != active_chat_id:
                # Keep a single delivery session for deterministic ordering.
                continue

            document = message.get("document")
            text = (message.get("text") or "").strip()

            if chat_id not in instructed_chats or text.startswith("/start"):
                try:
                    bot.send_message(int(chat_id), welcome if stage == "archive" else (
                        "Archive received.\n\nNow send config.json as a document."
                    ))
                except TelegramAPIError as exc:
                    log(f"Could not send instructions: {exc}")
                instructed_chats.add(int(chat_id))
                if not document:
                    continue

            if not document:
                try:
                    if stage == "archive":
                        bot.send_message(
                            int(chat_id),
                            "Please send tg-cl.tar.gz as a Telegram document.",
                        )
                    else:
                        bot.send_message(
                            int(chat_id),
                            "Please send config.json as a Telegram document.",
                        )
                except TelegramAPIError:
                    pass
                continue

            file_name = document.get("file_name") or "(unnamed)"
            file_id = document.get("file_id")
            if not file_id:
                continue

            if stage == "archive":
                if not is_tar_gz_document(document):
                    try:
                        bot.send_message(
                            int(chat_id),
                            f"Expected tg-cl.tar.gz, got “{file_name}”. "
                            "Please send the archive first.",
                        )
                    except TelegramAPIError:
                        pass
                    continue

                log(f"Downloading archive: {file_name}")
                size = bot.download_file(file_id, archive_path)
                log(f"Saved {archive_path.name} ({format_size(size)})")
                try:
                    bot.send_message(
                        int(chat_id),
                        f"✓ Received {file_name} ({format_size(size)}).\n\n"
                        "Now send config.json as a document.",
                    )
                except TelegramAPIError as exc:
                    log(f"Ack message failed: {exc}")
                stage = "config"
                log("Waiting for config.json …")
                continue

            if stage == "config":
                if not is_config_document(document):
                    try:
                        bot.send_message(
                            int(chat_id),
                            f"Expected config.json, got “{file_name}”. "
                            "Please send the JSON config file.",
                        )
                    except TelegramAPIError:
                        pass
                    continue

                log(f"Downloading config: {file_name}")
                size = bot.download_file(file_id, config_path)
                log(f"Saved {config_path.name} ({format_size(size)})")
                validate_config_json(config_path)
                try:
                    bot.send_message(
                        int(chat_id),
                        "✓ config.json received and validated.\n\n"
                        "Delivery complete. Starting TG Clone Pro on the runner.\n"
                        "This delivery bot will not be used again for this job.",
                    )
                except TelegramAPIError as exc:
                    log(f"Final ack failed: {exc}")
                log("Delivery complete")
                return archive_path, config_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Receive tg-cl.tar.gz and config.json via Telegram Bot API",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        help="Delivery bot token (or TELEGRAM_BOT_TOKEN env)",
    )
    parser.add_argument(
        "--out-dir",
        default=os.environ.get("DELIVERY_DIR", "delivery"),
        help="Directory where received files are stored",
    )
    parser.add_argument(
        "--allowed-user-ids",
        default=os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""),
        help="Comma-separated Telegram user IDs allowed to deliver files",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("DELIVERY_TIMEOUT_SECONDS", DEFAULT_OVERALL_TIMEOUT)),
        help="Max seconds to wait for both files (default: 1800)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.token:
        log("ERROR: TELEGRAM_BOT_TOKEN is required")
        return 2

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        allowed = parse_allowed_ids(args.allowed_user_ids)
    except ValueError as exc:
        log(f"ERROR: invalid TELEGRAM_ALLOWED_USER_IDS: {exc}")
        return 2

    if allowed:
        log(f"Restricting delivery to user IDs: {sorted(allowed)}")
    else:
        log("WARNING: no TELEGRAM_ALLOWED_USER_IDS set — first sender is accepted")

    bot = DeliveryBot(args.token, allowed)
    try:
        bot.bootstrap()
        archive, config = receive_files(bot, out_dir, args.timeout)
    except (TelegramAPIError, TimeoutError, RuntimeError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return 1

    # Marker file for the orchestrator (absolute paths).
    manifest = {
        "archive": str(archive),
        "config": str(config),
        "received_at": int(time.time()),
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(f"Manifest written: {manifest_path}")
    # Explicit success lines parsed by the shell entrypoint if needed.
    print(f"ARCHIVE_PATH={archive}", flush=True)
    print(f"CONFIG_PATH={config}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
