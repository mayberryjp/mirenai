from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from mirenai.config import settings

metadata = MetaData()
blocklist_metadata = MetaData()
hosts_metadata = MetaData()


class Base(DeclarativeBase):
    metadata = metadata


class BlocklistBase(DeclarativeBase):
    """Declarative base for tables that live in the separate blocklist database."""

    metadata = blocklist_metadata


class HostsBase(DeclarativeBase):
    """Declarative base for tables that live in the separate localhosts database."""

    metadata = hosts_metadata


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_blocklist_engine: Engine | None = None
_blocklist_session_factory: sessionmaker[Session] | None = None
_hosts_engine: Engine | None = None
_hosts_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
    return _engine


def get_blocklist_engine() -> Engine:
    global _blocklist_engine
    if _blocklist_engine is None:
        _blocklist_engine = create_engine(
            settings.blocklist_database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
    return _blocklist_engine


def get_hosts_engine() -> Engine:
    global _hosts_engine
    if _hosts_engine is None:
        _hosts_engine = create_engine(
            settings.localhosts_database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
    return _hosts_engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def get_blocklist_session_factory() -> sessionmaker[Session]:
    global _blocklist_session_factory
    if _blocklist_session_factory is None:
        _blocklist_session_factory = sessionmaker(
            bind=get_blocklist_engine(), expire_on_commit=False, future=True
        )
    return _blocklist_session_factory


def get_hosts_session_factory() -> sessionmaker[Session]:
    global _hosts_session_factory
    if _hosts_session_factory is None:
        _hosts_session_factory = sessionmaker(
            bind=get_hosts_engine(), expire_on_commit=False, future=True
        )
    return _hosts_session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def blocklist_session_scope() -> Iterator[Session]:
    session = get_blocklist_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def hosts_session_scope() -> Iterator[Session]:
    session = get_hosts_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:
        return False, f"database check failed: {type(exc).__name__}"


def init_db() -> None:
    import mirenai.repository.models  # noqa: F401  (register tables on Base.metadata)

    Base.metadata.create_all(get_engine())
    BlocklistBase.metadata.create_all(get_blocklist_engine())
    HostsBase.metadata.create_all(get_hosts_engine())

    _ensure_hosts_icon_column()
    _ensure_hosts_mac_column()
    _ensure_hosts_excluded_column()
    _ensure_hosts_flag_new_domains_column()
    _ensure_blocklist_format_column()
    _ensure_client_hourly_stats_foreign_column()
    _ensure_query_log_response_column()
    _ensure_uncacheable_client_column()
    _ensure_local_zone_interval_seconds_column()

    from mirenai.repository.blocklists import ensure_default_blocklist
    from mirenai.repository.upstreams import ensure_default_upstream

    ensure_default_blocklist()
    ensure_default_upstream()


def _ensure_hosts_icon_column() -> None:
    """Add ``hosts.icon`` to a localhosts database created before the column existed.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``hosts`` table needs a one-off ``ALTER TABLE``.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added it).
    """
    with get_hosts_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(hosts)"))}
        if columns and "icon" not in columns:
            conn.execute(text("ALTER TABLE hosts ADD COLUMN icon VARCHAR(255)"))


def _ensure_hosts_mac_column() -> None:
    """Add ``hosts.mac_address`` to a localhosts database created before the column existed.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``hosts`` table needs a one-off ``ALTER TABLE``.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added it).
    """
    with get_hosts_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(hosts)"))}
        if columns and "mac_address" not in columns:
            conn.execute(text("ALTER TABLE hosts ADD COLUMN mac_address VARCHAR(64)"))


def _ensure_hosts_excluded_column() -> None:
    """Add ``hosts.excluded_from_blocklist`` to a localhosts database created before it.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``hosts`` table needs a one-off ``ALTER TABLE``.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added it).
    """
    with get_hosts_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(hosts)"))}
        if columns and "excluded_from_blocklist" not in columns:
            conn.execute(
                text("ALTER TABLE hosts ADD COLUMN excluded_from_blocklist BOOLEAN NOT NULL DEFAULT 0")
            )


def _ensure_hosts_flag_new_domains_column() -> None:
    """Add ``hosts.flag_new_domains`` to a localhosts database created before it.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``hosts`` table needs a one-off ``ALTER TABLE``.
    Defaults to ``1`` so existing clients keep surfacing new domains. Idempotent
    and a no-op on fresh databases (where ``create_all`` already added it).
    """
    with get_hosts_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(hosts)"))}
        if columns and "flag_new_domains" not in columns:
            conn.execute(
                text("ALTER TABLE hosts ADD COLUMN flag_new_domains BOOLEAN NOT NULL DEFAULT 1")
            )


def _ensure_blocklist_format_column() -> None:
    """Add ``blocklists.format`` to a config database created before the column existed.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``blocklists`` table needs a one-off ``ALTER TABLE``.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added it).
    """
    with get_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(blocklists)"))}
        if columns and "format" not in columns:
            conn.execute(text("ALTER TABLE blocklists ADD COLUMN format VARCHAR(16)"))


def _ensure_client_hourly_stats_foreign_column() -> None:
    """Add ``client_hourly_stats.foreign`` to a config database created before it.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``client_hourly_stats`` table needs a one-off
    ``ALTER TABLE``. ``foreign`` is a SQL keyword, so it must stay quoted.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added it).
    """
    with get_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(client_hourly_stats)"))}
        if columns and "foreign" not in columns:
            conn.execute(
                text('ALTER TABLE client_hourly_stats ADD COLUMN "foreign" BIGINT NOT NULL DEFAULT 0')
            )


def _ensure_query_log_response_column() -> None:
    """Add ``query_log.last_response`` to a config database created before it.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``query_log`` table needs a one-off ``ALTER TABLE``.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added it).
    """
    with get_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(query_log)"))}
        if columns and "last_response" not in columns:
            conn.execute(text("ALTER TABLE query_log ADD COLUMN last_response TEXT"))


def _ensure_uncacheable_client_column() -> None:
    """Add per-client attribution to ``uncacheable_responses``.

    The table originally keyed on ``(domain, qtype, reason)``; recording which
    client drove each cache-miss changes the key to ``(client, domain, qtype,
    reason)``. SQLite can't alter a table's UNIQUE constraint in place and this
    project has no migration tool, so a pre-existing table — which holds only
    rolling diagnostic aggregates — is dropped and recreated with the new schema.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added
    the column).
    """
    engine = get_engine()
    dropped = False
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(uncacheable_responses)"))}
        if columns and "client" not in columns:
            conn.execute(text("DROP TABLE uncacheable_responses"))
            dropped = True
    if dropped:
        Base.metadata.tables["uncacheable_responses"].create(engine, checkfirst=True)


def _ensure_local_zone_interval_seconds_column() -> None:
    """Rename ``local_zones.update_interval_hours`` to ``update_interval_seconds``.

    The cadence was briefly shipped in hours and is now stored in seconds.
    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``local_zones`` table needs a one-off ``RENAME COLUMN``.
    Idempotent and a no-op on fresh databases (where ``create_all`` already added
    the seconds column).
    """
    with get_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(local_zones)"))}
        has_old = "update_interval_hours" in columns
        has_new = "update_interval_seconds" in columns
        if columns and has_old and not has_new:
            conn.execute(
                text(
                    "ALTER TABLE local_zones "
                    "RENAME COLUMN update_interval_hours TO update_interval_seconds"
                )
            )
