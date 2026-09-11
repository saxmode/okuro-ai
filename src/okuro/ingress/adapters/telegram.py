# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Telegram ingress adapter — long-poll getUpdates, allowlist
#   enforcement, normalize text into IngressMessage. Voice/photo/file
#   are deferred (step 7); only text + dictation transcripts are
#   accepted today.
# index: imports | constants | TelegramAdapter |
#   _fetch_updates | _process_update | _bootstrap_chat | _send_message
# AGENT_HEADER_END -->
"""Telegram Bot API adapter (long-poll).

Token storage:
    okuro.keyring entry ``integration/telegram/bot_token``

Config (rows in ``integrations`` table, config_json):
    {
      "allowed_chat_ids":  [int],   # approved senders
      "pending_chat_id":   int | null,  # first sender awaiting UI approval
      "offset":            int,     # getUpdates pagination cursor
      "bot_username":      str | null
    }

Allowlist semantics:
    - empty + pending_chat_id None  → bootstrap: first incoming chat is
      captured into pending_chat_id, message body is dropped (not routed),
      bot replies "Awaiting approval in okuro UI".
    - chat_id in allowed_chat_ids   → message envelope handed to router.
    - else                          → silent drop (logged at debug).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from okuro.keyring.storage import KeyringStorage

from ..adapter import IngressAdapter, IngressMessage, OnMessage, utc_iso
from .. import storage

log = logging.getLogger("okuro.ingress.telegram")

API_BASE = "https://api.telegram.org"
KEYRING_TOKEN_NAME = "integration/telegram/bot_token"

# Telegram's getUpdates long-poll: server holds the connection up to
# `timeout` seconds. The HTTP client timeout has to be slightly larger
# or every poll cycle ends in a ReadTimeout.
LONG_POLL_TIMEOUT = 30
HTTP_TIMEOUT = LONG_POLL_TIMEOUT + 10

# Voice → STT (Groq Whisper) — chosen because the user already has
# groq_api_key in the keyring and Whisper-large-v3 on Groq is fast +
# cheap. bridge_invoke is text-only, so we call the Groq REST endpoint
# directly. Duration cap keeps cost predictable and matches the
# step-7 design constraint.
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3"
GROQ_KEYRING_NAME = "groq_api_key"
VOICE_MAX_DURATION_SECONDS = 60
STT_HTTP_TIMEOUT = 60

# Telegram-side rate ceiling for getUpdates is generous, but we don't
# want to hammer it on 401/network errors. Adapter restart-with-backoff
# lives in the supervisor; this is just the in-loop sleep on transient
# error before retrying the next poll.
ERROR_BACKOFF_SECONDS = 5.0

# Voice transcription via Groq Whisper Large v3. Cheap + fast + groq_api_key
# is already in the keyring. bridge_invoke can't be used here because it
# is text-only; STT needs a multipart file POST.
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3"
VOICE_MAX_DURATION_SECONDS = 60
VOICE_MAX_BYTES = 25 * 1024 * 1024  # Groq cap


class TelegramAdapter(IngressAdapter):
    """Long-polling Telegram bot adapter — text messages only."""

    name = "telegram"

    def __init__(self) -> None:
        self._state: str = "stopped"
        self._last_poll_at: Optional[str] = None
        self._last_message_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[str] = None
        self._token: Optional[str] = None
        self._offset: int = 0
        self._allowed: list[int] = []
        self._pending: Optional[int] = None

    # ------------------------------------------------------------------
    # IngressAdapter contract
    # ------------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event, on_message: OnMessage) -> None:
        self._state = "starting"
        self._load_token()
        if not self._token:
            raise RuntimeError(
                "Telegram bot token missing — set integration/telegram/bot_token "
                "in the okuro keyring before enabling the adapter."
            )

        self._load_config()
        storage.mark_status(self.name, "running")
        storage.clear_error(self.name)
        self._state = "running"
        log.info(
            "telegram adapter starting (offset=%d, allowed=%s, pending=%s)",
            self._offset, self._allowed, self._pending,
        )

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            while not stop_event.is_set():
                # Hot-reload allowlist/pending so UI approvals take effect
                # on the very next poll without a daemon restart.
                self._load_config()
                try:
                    updates = await self._fetch_updates(client)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    log.warning("telegram poll network error: %s", exc)
                    self._record_error(f"network: {exc}")
                    await self._wait_or_stop(stop_event, ERROR_BACKOFF_SECONDS)
                    continue
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else 0
                    if status == 401:
                        # Bad token — no point looping. Let the supervisor
                        # surface this as a fatal adapter error.
                        raise RuntimeError(
                            "Telegram getUpdates returned 401 — bot token is "
                            "invalid or revoked."
                        ) from exc
                    log.warning("telegram poll HTTP %s: %s", status, exc)
                    self._record_error(f"http {status}: {exc}")
                    await self._wait_or_stop(stop_event, ERROR_BACKOFF_SECONDS)
                    continue

                self._last_poll_at = utc_iso()
                storage.mark_seen(self.name, message=bool(updates))

                for update in updates:
                    try:
                        await self._process_update(update, client, on_message)
                    except Exception:
                        # Per-update failure shouldn't kill the poll loop.
                        log.exception(
                            "failed to process telegram update %s",
                            update.get("update_id"),
                        )

                # No artificial sleep — getUpdates already long-poll-blocks.

        self._state = "stopped"
        storage.mark_status(self.name, "stopped")
        log.info("telegram adapter stopped")

    def health(self) -> dict:
        return {
            "name": self.name,
            "state": self._state,
            "last_poll_at": self._last_poll_at,
            "last_message_at": self._last_message_at,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "offset": self._offset,
            "allowed_count": len(self._allowed),
            "pending_chat_id": self._pending,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_token(self) -> None:
        ks = KeyringStorage()
        self._token = ks.get_key(KEYRING_TOKEN_NAME)

    def _load_config(self) -> None:
        row = storage.get_integration(self.name)
        cfg = row.config if row else {}
        self._allowed = [int(x) for x in cfg.get("allowed_chat_ids") or []]
        self._pending = cfg.get("pending_chat_id")
        self._offset = int(cfg.get("offset") or 0)

    async def _fetch_updates(self, client: httpx.AsyncClient) -> list[dict]:
        url = f"{API_BASE}/bot{self._token}/getUpdates"
        params = {
            "timeout": LONG_POLL_TIMEOUT,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset:
            params["offset"] = self._offset
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram getUpdates not ok: {body}")
        return list(body.get("result") or [])

    async def _process_update(
        self,
        update: dict,
        client: httpx.AsyncClient,
        on_message: OnMessage,
    ) -> None:
        update_id = int(update.get("update_id", 0))
        if update_id >= self._offset:
            self._offset = update_id + 1
            storage.patch_config(self.name, {"offset": self._offset})

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            log.debug("skipping update %s with no chat_id", update_id)
            return
        chat_id = int(chat_id)

        # Resolve the message text. Three sources, in order:
        #   1. Plain text message
        #   2. Voice note (download + transcribe via Groq Whisper)
        #   3. Caption on a media message (treat as text)
        text = (message.get("text") or message.get("caption") or "").strip()

        voice = message.get("voice")
        if not text and voice and chat_id in self._allowed:
            text = await self._maybe_transcribe_voice(client, chat_id, voice)
            if text is None:
                # Adapter already replied with the reason — abort.
                return

        if not text:
            # Unsupported media (photo/sticker/video without caption) from
            # an allowed chat → tell the sender; from anyone else → drop.
            if chat_id in self._allowed:
                kinds = [k for k in ("photo", "video", "sticker", "document",
                                     "animation", "video_note", "audio")
                         if k in message]
                hint = (
                    f"received {kinds[0]} — only text and voice notes (≤"
                    f"{VOICE_MAX_DURATION_SECONDS}s) are supported."
                    if kinds else
                    "received an empty/unsupported message."
                )
                await self._send_message(client, chat_id, hint)
            log.debug("dropped non-text/non-voice update %s", update_id)
            return

        if chat_id in self._allowed:
            async def _reply(reply_text: str) -> None:
                await self._send_message(client, chat_id, reply_text)
            envelope = IngressMessage(
                channel=self.name,
                from_id=str(chat_id),
                text=text,
                ts=_parse_ts(message.get("date")),
                raw=message,
                reply=_reply,
            )
            self._last_message_at = utc_iso()
            log.info("telegram envelope from %s (%d chars)", chat_id, len(text))
            await on_message(envelope)
            return

        # Unknown sender. Two sub-cases:
        if self._pending is None and not self._allowed:
            # Bootstrap path — capture the chat_id, reply once, wait for
            # the user to approve it in the web UI.
            self._pending = chat_id
            storage.patch_config(self.name, {"pending_chat_id": chat_id})
            await self._send_message(
                client,
                chat_id,
                "okuro: this chat is not yet approved. Your chat_id has been "
                f"captured ({chat_id}). An operator must approve it before "
                "messages are routed.",
            )
            log.info("telegram captured pending chat_id %s", chat_id)
            return

        # Either there's already a pending chat (someone else is trying)
        # or the allowlist exists and this chat isn't in it. Silent drop.
        log.debug("telegram dropped message from non-allowlisted chat %s", chat_id)

    async def _send_message(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        text: str,
    ) -> None:
        try:
            url = f"{API_BASE}/bot{self._token}/sendMessage"
            await client.post(url, json={"chat_id": chat_id, "text": text})
        except Exception:
            log.exception("failed to send telegram reply to %s", chat_id)

    async def _maybe_transcribe_voice(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        voice: dict,
    ) -> Optional[str]:
        """Download the OGG payload and route it through Groq Whisper.

        Returns the transcript on success, or ``None`` when the user has
        already been notified (duration cap exceeded / no API key /
        download or STT failure). Returning ``None`` signals the caller
        to abort the update — no envelope, no further drop reply.
        """
        duration = int(voice.get("duration") or 0)
        if duration > VOICE_MAX_DURATION_SECONDS:
            await self._send_message(
                client, chat_id,
                f"voice note too long ({duration}s) — max "
                f"{VOICE_MAX_DURATION_SECONDS}s.",
            )
            return None

        file_id = voice.get("file_id")
        if not file_id:
            await self._send_message(client, chat_id, "voice payload missing file_id.")
            return None

        groq_key = KeyringStorage().get_key(GROQ_KEYRING_NAME)
        if not groq_key:
            await self._send_message(
                client, chat_id,
                "STT unavailable: groq_api_key missing from okuro keyring.",
            )
            return None

        try:
            # 1. getFile to resolve the storage path
            gf = await client.get(
                f"{API_BASE}/bot{self._token}/getFile",
                params={"file_id": file_id},
            )
            gf.raise_for_status()
            file_path = (gf.json().get("result") or {}).get("file_path")
            if not file_path:
                raise RuntimeError("getFile returned no file_path")

            # 2. Download the OGG bytes from Telegram's file CDN.
            dl = await client.get(
                f"{API_BASE}/file/bot{self._token}/{file_path}",
                timeout=STT_HTTP_TIMEOUT,
            )
            dl.raise_for_status()
            audio_bytes = dl.content
            mime = voice.get("mime_type") or "audio/ogg"
            # Telegram serves voice notes as .oga; Groq's Whisper rejects
            # that extension even though the bytes are valid OGG/OPUS.
            # Force .ogg so the upload sniff passes.
            filename = "voice.ogg"

            # 3. POST to Groq Whisper (OpenAI-compatible endpoint).
            stt = await client.post(
                GROQ_STT_URL,
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": (filename, audio_bytes, mime)},
                data={"model": GROQ_STT_MODEL, "response_format": "json"},
                timeout=STT_HTTP_TIMEOUT,
            )
            stt.raise_for_status()
            transcript = (stt.json().get("text") or "").strip()
            if not transcript:
                await self._send_message(
                    client, chat_id, "voice transcribed to empty text — try again."
                )
                return None
            log.info(
                "telegram voice transcribed (%ds → %d chars) for chat %s",
                duration, len(transcript), chat_id,
            )
            return transcript
        except Exception as exc:
            log.exception("voice transcription failed for chat %s", chat_id)
            await self._send_message(
                client, chat_id,
                f"voice transcription failed: {type(exc).__name__}",
            )
            return None

    def _record_error(self, msg: str) -> None:
        self._last_error = msg[:500]
        self._last_error_at = utc_iso()
        storage.mark_error(self.name, msg)

    async def _wait_or_stop(self, stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def _parse_ts(value) -> datetime:
    """Telegram message.date is unix-seconds. Fall back to now() on miss."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
