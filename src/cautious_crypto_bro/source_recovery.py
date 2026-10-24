from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from telethon.errors import SessionPasswordNeededError, UnauthorizedError

from .telegram_source import SourceAuthorizationRequired

if TYPE_CHECKING:
    from .approval_bot import ApprovalBot
    from .telegram_source import TelegramSource

logger = logging.getLogger(__name__)


def _authorization_failure(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return any(_authorization_failure(child) for child in error.exceptions)
    return isinstance(error, (SourceAuthorizationRequired, UnauthorizedError))


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return _failure_reason(error.exceptions[0])
    if isinstance(error, SourceAuthorizationRequired):
        return str(error)
    return type(error).__name__


def read_source_identity(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open("rb") as source:
        data = source.read(129)
    if len(data) > 128:
        raise ValueError("Invalid source identity record")
    user_id = json.loads(data)["user_id"]
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Invalid source identity record")
    return user_id


def pin_source_identity(path: Path, user_id: int) -> None:
    expected = read_source_identity(path)
    if expected is not None and user_id != expected:
        raise SourceAuthorizationRequired(
            "Source account identity mismatch; local repair required"
        )
    if expected is None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"user_id": user_id}), encoding="utf-8")
        temporary.replace(path)


class SourceRecovery:
    """Keep operator control and position protection alive during reader outages."""

    def __init__(
        self,
        source: TelegramSource,
        bot: ApprovalBot,
        identity_path: Path,
        operator_id: int,
        *,
        retry_seconds: float = 30,
        alert_seconds: float = 900,
        auth_retry_seconds: float = 300,
        read_check_seconds: float = 30,
    ) -> None:
        self._source = source
        self._bot = bot
        self._identity_path = identity_path
        self._operator_id = operator_id
        self._retry_seconds = retry_seconds
        self._alert_seconds = alert_seconds
        self._auth_retry_seconds = auth_retry_seconds
        self._read_check_seconds = read_check_seconds
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._next_alert = 0.0
        self._outage = False
        self._manual_reason: str | None = None
        self._next_login = 0.0
        self.state = "STARTING"
        self.reason = ""
        self.last_online: datetime | None = None

    def status(self) -> str:
        last = (
            self.last_online.isoformat(timespec="seconds")
            if self.last_online
            else "not yet"
        )
        return (
            f"Telegram reader: {self.state}\n"
            f"Last connected: {last}\n"
            f"Detail: {self.reason or 'none'}\n"
            "Use /source_login in the bot's private chat if authorization is required."
        )

    def _expected_user(self) -> int | None:
        return read_source_identity(self._identity_path)

    def _pin_user(self, user_id: int) -> None:
        pin_source_identity(self._identity_path, user_id)

    async def _alert(self) -> None:
        if monotonic() < self._next_alert:
            return
        try:
            await self._bot.send_source_notice(
                "⚠️ TELEGRAM READER NEEDS ATTENTION — HUMAN REVIEW REQUIRED\n"
                f"{self.state}: {self.reason}\n"
                + (
                    "Channel history or post processing failed; unaffected channels continue. "
                    if self.state == "DEGRADED"
                    else "New signal ingestion is unavailable. "
                )
                + "The bot and position supervisor "
                "remain active. Network reconnect retries every 30 seconds; "
                "authorization is rechecked every five minutes.\n"
                "Use /source for status; use /source_login privately for QR authorization. "
                "Never send login codes or a 2FA password to this bot."
            )
        except Exception as exc:
            logger.warning("Source outage notification failed: %s", type(exc).__name__)
        else:
            self._next_alert = monotonic() + self._alert_seconds

    async def _announce_restored(self) -> None:
        if self._outage:
            try:
                await self._bot.send_source_notice(
                    "✅ Telegram reader restored; channel catch-up resumed."
                )
            except Exception as exc:
                logger.warning(
                    "Source recovery notification failed: %s", type(exc).__name__
                )
            self._outage = False
        self._next_alert = 0

    async def _monitor_reads(self) -> None:
        while True:
            warnings = self._source.read_warnings()
            if warnings:
                self.state, self.reason = "DEGRADED", "; ".join(warnings)
                self._outage = True
                await self._alert()
            elif self.state == "DEGRADED":
                self.state, self.reason = "ONLINE", ""
                await self._announce_restored()
            await asyncio.sleep(self._read_check_seconds)

    async def _connected(self) -> None:
        user_id = await self._source.session_user_id()
        if user_id is None:
            raise SourceAuthorizationRequired("Telegram authorization required")
        self._pin_user(user_id)
        await self._source.start()
        warnings = self._source.read_warnings()
        self.state = "DEGRADED" if warnings else "ONLINE"
        self.reason = "; ".join(warnings)
        self._manual_reason = None
        self.last_online = datetime.now(UTC)
        if not warnings:
            await self._announce_restored()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._source.run_catchup())
            tasks.create_task(self._monitor_reads())
            await self._source.run_until_disconnected()

    async def run(self) -> None:
        while True:
            try:
                async with self._lock:
                    try:
                        await self._connected()
                    finally:
                        try:
                            await self._source.disconnect()
                        except Exception as exc:
                            logger.warning(
                                "Source disconnect failed: %s", type(exc).__name__
                            )
            except Exception as exc:
                state = "AUTH_REQUIRED" if _authorization_failure(exc) else "OFFLINE"
                if self.state != state:
                    self._next_alert = 0
                self.state = state
                # Exception strings can contain sensitive transport data.
                self.reason = self._manual_reason or _failure_reason(exc)
                self._outage = True
                logger.warning("Telegram reader unavailable: %s", self.reason)
                await self._alert()
            try:
                delay = (
                    self._auth_retry_seconds
                    if self.state == "AUTH_REQUIRED"
                    else self._retry_seconds
                )
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self._wake.clear()

    async def login(self) -> None:
        if self.state in {"ONLINE", "DEGRADED"}:
            await self._bot.send_source_notice(
                f"Session is already authorized; reader state {self.state}. "
                "Use /source for read errors. Session retained."
            )
            return
        if self._lock.locked():
            await self._bot.send_source_notice(
                "A reader connection/login attempt is already running."
            )
            return
        if monotonic() < self._next_login:
            await self._bot.send_source_notice(
                "Please wait 30 seconds before another login attempt."
            )
            return
        self._next_login = monotonic() + 30
        async with self._lock:
            self.state = "AUTHORIZING"
            try:
                user_id = await self._source.session_user_id()
                if user_id is not None:
                    self._pin_user(user_id)
                    return
                await self._qr_login()
            except SessionPasswordNeededError:
                self.state = "AUTH_REQUIRED"
                self.reason = self._manual_reason = (
                    "2FA requires local operator authorization"
                )
                await self._bot.send_source_notice(
                    "HUMAN ACTION REQUIRED: Telegram requires your 2FA password. "
                    "Stop the app, run scripts/authorize_telegram.py locally from its "
                    "active image, then restart the app. See README: Telegram reader recovery. "
                    "Do not send the password here."
                )
            except Exception as exc:
                self.state = "AUTH_REQUIRED"
                self.reason = _failure_reason(exc)
                await self._bot.send_source_notice(
                    "Authorization did not complete. Use /source for status and retry "
                    "/source_login, or use the local operator recovery procedure."
                )
            finally:
                self._wake.set()

    async def _qr_login(self) -> None:
        expected = self._expected_user() or self._operator_id
        self._pin_user(expected)
        for _ in range(3):
            qr = await self._source.login_qr()
            waiter = asyncio.create_task(qr.wait())
            message_id = None
            try:
                # Telethon must register its token update handler before display.
                await asyncio.sleep(0)
                message_id = await self._bot.show_source_login(qr.url)
                user = await asyncio.wait_for(waiter, timeout=45)
                if user.id != expected:
                    raise SourceAuthorizationRequired(
                        "Source account identity mismatch"
                    )
                self._pin_user(user.id)
                await self._bot.send_source_notice(
                    "Authorization accepted; verifying channel access."
                )
                return
            except TimeoutError:
                continue
            finally:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                if message_id is not None:
                    await self._bot.delete_source_login(message_id)
        raise SourceAuthorizationRequired("QR authorization expired")
