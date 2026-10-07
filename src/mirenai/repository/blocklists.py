"""Persistence for DNS blocklists.

Blocklist *configuration* (name, URL, cadence, status) lives in the main
configuration database; the downloaded *domains* live in a separate blocklist
database so a large list never bloats the config file. The two are joined by
``Blocklist.id`` / ``BlocklistDomain.blocklist_id``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, select

from mirenai.db import blocklist_session_scope, session_scope
from mirenai.domain.blocklist import domain_suffixes, is_blocked
from mirenai.repository.models import Blocklist, BlocklistDomain


def _to_dict(blocklist: Blocklist) -> dict[str, Any]:
    return {
        "id": blocklist.id,
        "name": blocklist.name,
        "url": blocklist.url,
        "update_interval_hours": blocklist.update_interval_hours,
        "enabled": blocklist.enabled,
        "domain_count": blocklist.domain_count,
        "last_downloaded_at": blocklist.last_downloaded_at.isoformat()
        if blocklist.last_downloaded_at is not None
        else None,
        "last_status": blocklist.last_status,
        "format": blocklist.format,
        "created_at": blocklist.created_at.isoformat(),
        "updated_at": blocklist.updated_at.isoformat(),
    }


def list_blocklists(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(Blocklist).order_by(Blocklist.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_blocklists() -> int:
    with session_scope() as session:
        return len(session.scalars(select(Blocklist.id)).all())


def get_blocklist(blocklist_id: int) -> dict[str, Any] | None:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        return _to_dict(blocklist) if blocklist is not None else None


def create_blocklist(data: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        blocklist = Blocklist(
            name=data["name"],
            url=data["url"],
            update_interval_hours=data.get("update_interval_hours", 24),
            enabled=data.get("enabled", True),
        )
        session.add(blocklist)
        session.flush()
        return _to_dict(blocklist)


def update_blocklist(blocklist_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return None
        for field in ("name", "url", "update_interval_hours", "enabled"):
            if field in data:
                setattr(blocklist, field, data[field])
        session.flush()
        return _to_dict(blocklist)


def delete_blocklist(blocklist_id: int) -> bool:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return False
        session.delete(blocklist)
    _delete_domains(blocklist_id)
    return True


def _delete_domains(blocklist_id: int) -> None:
    with blocklist_session_scope() as session:
        session.execute(
            delete(BlocklistDomain).where(BlocklistDomain.blocklist_id == blocklist_id)
        )


def replace_domains(blocklist_id: int, domains: Sequence[str]) -> None:
    """Swap a blocklist's stored domains to ``domains``, preserving ``first_seen``.

    Diffs against the currently stored set so a re-download keeps the original
    ``first_seen`` for domains that persist, stamps only genuinely new domains with
    the current time, and deletes domains no longer present. This is what makes the
    newest-entries feed meaningful; a blind delete-and-reinsert would reset every
    timestamp on every refresh.
    """
    now = datetime.now()
    desired = set(domains)
    with blocklist_session_scope() as session:
        existing = set(
            session.scalars(
                select(BlocklistDomain.domain).where(
                    BlocklistDomain.blocklist_id == blocklist_id
                )
            ).all()
        )
        removed = list(existing - desired)
        added = list(desired - existing)
        # Chunk the delete to stay under SQLite's host-parameter cap.
        for start in range(0, len(removed), _MATCH_CHUNK):
            chunk = removed[start : start + _MATCH_CHUNK]
            session.execute(
                delete(BlocklistDomain)
                .where(BlocklistDomain.blocklist_id == blocklist_id)
                .where(BlocklistDomain.domain.in_(chunk))
            )
        if added:
            session.execute(
                insert(BlocklistDomain),
                [
                    {"blocklist_id": blocklist_id, "domain": domain, "first_seen": now}
                    for domain in added
                ],
            )


def list_domains(
    blocklist_id: int, limit: int | None = None, offset: int = 0
) -> list[str]:
    stmt = (
        select(BlocklistDomain.domain)
        .where(BlocklistDomain.blocklist_id == blocklist_id)
        .order_by(BlocklistDomain.domain)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with blocklist_session_scope() as session:
        return list(session.scalars(stmt).all())


def count_domains(blocklist_id: int) -> int:
    stmt = select(BlocklistDomain.id).where(BlocklistDomain.blocklist_id == blocklist_id)
    with blocklist_session_scope() as session:
        return len(session.scalars(stmt).all())


def total_enabled_domain_count() -> int:
    """Total domains across *enabled* blocklists (the sum of each list's count).

    Cheap config-database read used to sample blocklist size over time. Counts a
    domain once per list it appears on (not de-duplicated across lists).
    """
    with session_scope() as session:
        total = session.execute(
            select(func.coalesce(func.sum(Blocklist.domain_count), 0)).where(
                Blocklist.enabled.is_(True)
            )
        ).scalar_one()
    return int(total)


def list_new_domains(
    limit: int | None = None, offset: int = 0, blocklist_id: int | None = None
) -> list[dict[str, Any]]:
    """Return stored domains ordered newest-first by ``first_seen``.

    Powers a "recently added blocklist entries" table. Domains live in the
    blocklist database while blocklist *names* live in the config database, so each
    row is resolved to its list via a separate ``blocklist_id`` -> name lookup.
    """
    stmt = select(
        BlocklistDomain.blocklist_id, BlocklistDomain.domain, BlocklistDomain.first_seen
    )
    if blocklist_id is not None:
        stmt = stmt.where(BlocklistDomain.blocklist_id == blocklist_id)
    stmt = stmt.order_by(BlocklistDomain.first_seen.desc(), BlocklistDomain.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with blocklist_session_scope() as session:
        rows = session.execute(stmt).all()
    if not rows:
        return []
    names = _blocklist_names({bid for bid, _, _ in rows})
    return [
        {
            "domain": dom,
            "blocklist_id": bid,
            "blocklist_name": names.get(bid),
            "first_seen": seen.isoformat(),
        }
        for bid, dom, seen in rows
    ]


def count_new_domains(blocklist_id: int | None = None) -> int:
    stmt = select(BlocklistDomain.id)
    if blocklist_id is not None:
        stmt = stmt.where(BlocklistDomain.blocklist_id == blocklist_id)
    with blocklist_session_scope() as session:
        return len(session.scalars(stmt).all())


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _blocklist_names(ids: set[int]) -> dict[int, str]:
    """Map blocklist ids to their names (config database)."""
    if not ids:
        return {}
    with session_scope() as session:
        rows = session.execute(select(Blocklist.id, Blocklist.name).where(Blocklist.id.in_(ids)))
        return {row_id: name for row_id, name in rows}


def search_domains(
    search: str, limit: int | None = None, offset: int = 0
) -> list[dict[str, Any]]:
    """Find stored domains matching ``search`` (case-insensitive substring).

    Domains live in the blocklist database while blocklist *names* live in the
    config database, so each match is resolved to the list it belongs to via a
    separate ``blocklist_id`` -> name lookup.
    """
    term = f"%{_escape_like(search)}%"
    stmt = (
        select(BlocklistDomain.blocklist_id, BlocklistDomain.domain)
        .where(BlocklistDomain.domain.ilike(term, escape="\\"))
        .order_by(BlocklistDomain.domain, BlocklistDomain.blocklist_id)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with blocklist_session_scope() as session:
        rows = session.execute(stmt).all()
    if not rows:
        return []
    names = _blocklist_names({blocklist_id for blocklist_id, _ in rows})
    return [
        {
            "domain": domain,
            "blocklist_id": blocklist_id,
            "blocklist_name": names.get(blocklist_id),
        }
        for blocklist_id, domain in rows
    ]


def count_domain_matches(search: str) -> int:
    term = f"%{_escape_like(search)}%"
    stmt = select(BlocklistDomain.id).where(BlocklistDomain.domain.ilike(term, escape="\\"))
    with blocklist_session_scope() as session:
        return len(session.scalars(stmt).all())


def record_success(blocklist_id: int, domain_count: int, source_format: str) -> None:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return
        blocklist.domain_count = domain_count
        blocklist.format = source_format
        blocklist.last_status = f"ok: {domain_count} domains"
        blocklist.last_downloaded_at = datetime.now()


def record_failure(blocklist_id: int, message: str) -> None:
    """Record a failed download. Existing domains are left in place."""
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return
        blocklist.last_status = f"error: {message}"[:255]
        blocklist.last_downloaded_at = datetime.now()


def load_blocklist_domains() -> frozenset[str]:
    """Return every domain belonging to an enabled blocklist, for the DNS server."""
    with session_scope() as session:
        enabled_ids = list(
            session.scalars(select(Blocklist.id).where(Blocklist.enabled.is_(True))).all()
        )
    if not enabled_ids:
        return frozenset()
    with blocklist_session_scope() as session:
        rows = session.scalars(
            select(BlocklistDomain.domain).where(BlocklistDomain.blocklist_id.in_(enabled_ids))
        ).all()
    return frozenset(rows)


# SQLite caps host parameters per statement (historically 999); chunk lookups to stay under it.
_MATCH_CHUNK = 500


def find_blocked_domains(candidates: Iterable[str]) -> frozenset[str]:
    """Return the subset of ``candidates`` listed verbatim on an *enabled* blocklist.

    Exact matches only -- the caller expands a query name into its parent suffixes
    (see :func:`mirenai.domain.blocklist.domain_suffixes`). Looks up just the
    candidate rows through the indexed ``domain`` column, so annotating a page of
    results never loads the whole blocklist into memory.
    """
    unique = {candidate for candidate in candidates if candidate}
    if not unique:
        return frozenset()
    with session_scope() as session:
        enabled_ids = list(
            session.scalars(select(Blocklist.id).where(Blocklist.enabled.is_(True))).all()
        )
    if not enabled_ids:
        return frozenset()
    ordered = list(unique)
    matched: set[str] = set()
    with blocklist_session_scope() as session:
        for start in range(0, len(ordered), _MATCH_CHUNK):
            chunk = ordered[start : start + _MATCH_CHUNK]
            rows = session.scalars(
                select(BlocklistDomain.domain)
                .where(BlocklistDomain.blocklist_id.in_(enabled_ids))
                .where(BlocklistDomain.domain.in_(chunk))
            ).all()
            matched.update(rows)
    return frozenset(matched)


def find_domain_sources(candidates: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Map each blocked name in ``candidates`` to the enabled blocklist(s) listing it.

    A name is blocked when it or any parent suffix is on an enabled blocklist (the
    resolver's suffix semantics). Returns a mapping from the original candidate name
    to a list of ``{blocklist_id, blocklist_name, matched_domain}`` sources, where
    ``matched_domain`` is the listed suffix that caused the block. Names that match
    nothing are absent from the result.
    """
    suffix_candidates: dict[str, set[str]] = {}
    for candidate in candidates:
        for suffix in domain_suffixes(candidate):
            suffix_candidates.setdefault(suffix, set()).add(candidate)
    if not suffix_candidates:
        return {}
    with session_scope() as session:
        enabled_ids = list(
            session.scalars(select(Blocklist.id).where(Blocklist.enabled.is_(True))).all()
        )
    if not enabled_ids:
        return {}
    suffixes = list(suffix_candidates)
    matches: list[tuple[int, str]] = []
    with blocklist_session_scope() as session:
        for start in range(0, len(suffixes), _MATCH_CHUNK):
            chunk = suffixes[start : start + _MATCH_CHUNK]
            rows = session.execute(
                select(BlocklistDomain.blocklist_id, BlocklistDomain.domain)
                .where(BlocklistDomain.blocklist_id.in_(enabled_ids))
                .where(BlocklistDomain.domain.in_(chunk))
            ).all()
            matches.extend((blocklist_id, domain) for blocklist_id, domain in rows)
    if not matches:
        return {}
    names = _blocklist_names({blocklist_id for blocklist_id, _ in matches})
    sources: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[tuple[int, str]]] = {}
    for blocklist_id, matched_domain in matches:
        for candidate in suffix_candidates.get(matched_domain, ()):
            key = (blocklist_id, matched_domain)
            dedupe = seen.setdefault(candidate, set())
            if key in dedupe:
                continue
            dedupe.add(key)
            sources.setdefault(candidate, []).append(
                {
                    "blocklist_id": blocklist_id,
                    "blocklist_name": names.get(blocklist_id),
                    "matched_domain": matched_domain,
                }
            )
    return sources


def annotate_blocked(rows: list[dict[str, Any]], key: str = "domain") -> list[dict[str, Any]]:
    """Add a boolean ``blocked`` field to each row from ``row[key]``.

    Matches the resolver's suffix semantics (a name is blocked when it or any
    parent domain is on an enabled blocklist). Resolves the whole batch in one
    lookup, then mutates and returns ``rows``.
    """
    matched = find_blocked_domains(
        suffix for row in rows for suffix in domain_suffixes(row[key])
    )
    for row in rows:
        row["blocked"] = is_blocked(matched, row[key])
    return rows


def ensure_default_blocklist() -> None:
    """Seed a single default blocklist (HaGeZi Multi PRO) when none exist.

    Added disabled so a fresh install ships a curated list ready to enable, without
    blocking anything until the operator opts in. Idempotent: skipped once any
    blocklist is configured.
    """
    with session_scope() as session:
        if session.scalars(select(Blocklist.id)).first() is not None:
            return
        session.add(
            Blocklist(
                name="HaGeZi Multi PRO",
                url="https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/pro.txt",
                enabled=False,
            )
        )
