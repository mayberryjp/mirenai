"""Persistent first-seen detection and Discord delivery state."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.notifications import DomainObservation
from mirenai.integrations.discord import DiscordDeliveryError, send_notification
from mirenai.logging import get_logger
from mirenai.repository.hosts import load_device_name, load_new_domain_unmonitored
from mirenai.repository.models import ClientRequest, DomainNotification
from mirenai.repository.settings import load_runtime_settings

log = get_logger("discord.notifications")
_MAX_ATTEMPTS = 5


def seed_seen_domains() -> None:
    """Baseline existing request history without generating retrospective alerts."""
    with session_scope() as session:
        source = select(
            ClientRequest.client, ClientRequest.domain,
            literal(""), literal(""), literal(""),
            func.min(ClientRequest.first_seen), literal("skipped"), literal(0),
            func.min(ClientRequest.first_seen),
        ).group_by(ClientRequest.client, ClientRequest.domain)
        session.execute(
            sqlite_insert(DomainNotification).from_select(
                ["client", "domain", "qtype", "action", "rcode", "first_seen",
                 "status", "attempts", "next_attempt"],
                source,
            ).on_conflict_do_nothing(index_elements=["client", "domain"])
        )


def record_observations(events: list[DomainObservation]) -> None:
    if not events:
        return
    settings = load_runtime_settings()
    excluded = load_new_domain_unmonitored()
    with session_scope() as session:
        for event in events:
            enabled = (
                event.notifications_enabled and settings.discord_notifications_enabled
                and bool(settings.discord_webhook_url) and event.client not in excluded
            )
            query_type_enabled = (
                event.query_type_enabled and settings.discord_query_types.get(event.qtype, True)
            )
            status = "skipped" if not enabled else "pending" if query_type_enabled else "filtered"
            stmt = sqlite_insert(DomainNotification).values(
                client=event.client, domain=event.domain, qtype=event.qtype,
                action=event.action, rcode=event.rcode, first_seen=event.first_seen,
                status=status, attempts=0, next_attempt=event.first_seen,
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["client", "domain"],
                    set_={
                        "qtype": stmt.excluded.qtype, "action": stmt.excluded.action,
                        "rcode": stmt.excluded.rcode, "first_seen": stmt.excluded.first_seen,
                        "status": stmt.excluded.status, "attempts": 0,
                        "next_attempt": stmt.excluded.next_attempt,
                    },
                    where=(
                        (DomainNotification.status == "filtered")
                        & (stmt.excluded.status != "filtered")
                    ),
                )
            )


def deliver_pending() -> float:
    """Send at most one message; return a global delivery backoff in seconds.

    Only the DNS worker calls this function, so there is a single queue consumer.
    HTTP runs outside the database transaction.
    """
    settings = load_runtime_settings()
    excluded = load_new_domain_unmonitored()
    with session_scope() as session:
        pending = DomainNotification.status == "pending"
        eligible = DomainNotification.status.in_(["pending", "filtered"])
        if not settings.discord_notifications_enabled or not settings.discord_webhook_url:
            session.execute(update(DomainNotification).where(eligible).values(status="skipped"))
            return 0.0
        if excluded:
            session.execute(
                update(DomainNotification)
                .where(eligible, DomainNotification.client.in_(excluded))
                .values(status="skipped")
            )
        disabled_types = [
            name for name, enabled in settings.discord_query_types.items() if not enabled
        ]
        if disabled_types:
            session.execute(
                update(DomainNotification)
                .where(pending, DomainNotification.qtype.in_(disabled_types))
                .values(status="filtered")
            )
        row = session.scalars(
            select(DomainNotification)
            .where(pending, DomainNotification.next_attempt <= datetime.now())
            .order_by(DomainNotification.id).limit(1)
        ).first()
        if row is None:
            return 0.0
        event = DomainObservation(
            row.client, row.domain, row.qtype, row.action, row.rcode, row.first_seen, True
        )
        row_id, attempts = row.id, row.attempts + 1
    delay = 0.0
    status = "sent"
    try:
        send_notification(
            settings.discord_webhook_url, event, client_name=load_device_name(event.client)
        )
    except DiscordDeliveryError as exc:
        delay = max(exc.retry_seconds or 0.0, min(300.0, 5.0 * 2 ** (attempts - 1)))
        status = "pending" if exc.retry_seconds is not None and attempts < _MAX_ATTEMPTS else "failed"
        log.warning(
            "Discord delivery %s client=%s domain=%s attempt=%d: %s",
            status, event.client, event.domain, attempts, exc,
        )
    with session_scope() as session:
        session.execute(
            update(DomainNotification).where(DomainNotification.id == row_id).values(
                status=status, attempts=attempts,
                next_attempt=datetime.now() + timedelta(seconds=delay),
            )
        )
    return delay
