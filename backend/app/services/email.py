"""
Outbound email.

Small on purpose: the application needs to deliver a handful of
transactional messages (workspace invitations today, password resets once
they are wired up), not to be a mail platform.

Two backends:

``console``
    Writes the message to the log instead of sending it. The default, and
    what a developer without an SMTP server gets. It is honest about what it
    did -- the log line says the message was not delivered.

``smtp``
    Real delivery through smtplib, with STARTTLS by default.

Sending never raises into a request. A failure to deliver an invitation must
not lose the invitation, which is already recorded and can be resent.
"""

from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("email")


class EmailSender(ABC):
    """Delivers one message."""

    name = "base"

    @abstractmethod
    def send(self, to: str, subject: str, body: str) -> bool:
        """Return True when the message was handed to a transport."""


class ConsoleSender(EmailSender):
    """Logs the message rather than sending it.

    Used when no SMTP server is configured. The log states plainly that
    nothing was delivered, so an operator reading it does not conclude the
    recipient received anything.
    """

    name = "console"

    def send(self, to: str, subject: str, body: str) -> bool:
        logger.warning(
            f"EMAIL NOT SENT (no SMTP configured). to={to!r} subject={subject!r}\n{body}",
            extra={"action": "email.not_sent"},
        )
        return False


class SMTPSender(EmailSender):
    """Delivers through an SMTP server."""

    name = "smtp"

    def send(self, to: str, subject: str, body: str) -> bool:
        message = EmailMessage()
        message["From"] = settings.EMAIL_FROM
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        try:
            with smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
            ) as server:
                if settings.SMTP_USE_TLS:
                    server.starttls()
                if settings.SMTP_USERNAME:
                    server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                server.send_message(message)
            logger.info(
                f"Sent {subject!r} to {to}",
                extra={"action": "email.sent"},
            )
            return True
        except Exception as exc:
            # Never propagates: the invitation is already stored and can be
            # resent. Losing it because a mail server hiccuped would be worse
            # than a message that has to be sent again.
            logger.error(
                f"Could not send {subject!r} to {to}: {exc}",
                extra={"action": "email.failed"},
                exc_info=True,
            )
            return False


_BACKENDS: dict[str, type[EmailSender]] = {
    "console": ConsoleSender,
    "smtp": SMTPSender,
}

_sender: EmailSender | None = None


def get_sender() -> EmailSender:
    """The configured sender, constructed once."""
    global _sender
    if _sender is None:
        backend = (settings.EMAIL_BACKEND or "console").strip().lower()
        sender_class = _BACKENDS.get(backend)
        if sender_class is None:
            logger.error(
                f"Unknown EMAIL_BACKEND {backend!r}; falling back to console. "
                f"Known backends: {', '.join(sorted(_BACKENDS))}"
            )
            sender_class = ConsoleSender
        _sender = sender_class()
    return _sender


def reset_sender() -> None:
    """Drop the cached sender so configuration changes take effect."""
    global _sender
    _sender = None


def send_email(to: str, subject: str, body: str) -> bool:
    """Send one message. Returns True only when a transport accepted it."""
    return get_sender().send(to, subject, body)
