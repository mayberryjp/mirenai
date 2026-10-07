import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from mirenai.config import settings
from mirenai.logging import get_logger

log = get_logger("db")


class ConfigBase(DeclarativeBase):
    """Operator-tunable configuration tables (``config.db``)."""

    metadata = MetaData()


class StatsBase(DeclarativeBase):
    """Query statistics and time-series tables (``stats.db``)."""

    metadata = MetaData()


class CacheBase(DeclarativeBase):
    """DNS answer-cache mirror tables (``cache.db``)."""

    metadata = MetaData()


class QueryLogBase(DeclarativeBase):
    """Per-query log tables (``querylogs.db``)."""

    metadata = MetaData()


class BlocklistBase(DeclarativeBase):
    """Declarative base for tables that live in the separate blocklist database."""

    metadata = MetaData()


class HostsBase(DeclarativeBase):
    """Declarative base for tables that live in the separate localhosts database."""

    metadata = MetaData()


_config_engine: Engine | None = None
_stats_engine: Engine | None = None
_cache_engine: Engine | None = None
_querylog_engine: Engine | None = None
_blocklist_engine: Engine | None = None
_hosts_engine: Engine | None = None

_session_factory: sessionmaker[Session] | None = None
_blocklist_session_factory: sessionmaker[Session] | None = None
_hosts_session_factory: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    return create_engine(url, connect_args={"check_same_thread": False}, future=True)


def get_config_engine() -> Engine:
    global _config_engine
    if _config_engine is None:
        _config_engine = _make_engine(settings.config_database_url)
    return _config_engine


def get_stats_engine() -> Engine:
    global _stats_engine
    if _stats_engine is None:
        _stats_engine = _make_engine(settings.stats_database_url)
    return _stats_engine


def get_cache_engine() -> Engine:
    global _cache_engine
    if _cache_engine is None:
        _cache_engine = _make_engine(settings.cache_database_url)
    return _cache_engine


def get_querylog_engine() -> Engine:
    global _querylog_engine
    if _querylog_engine is None:
        _querylog_engine = _make_engine(settings.querylog_database_url)
    return _querylog_engine


def get_blocklist_engine() -> Engine:
    global _blocklist_engine
    if _blocklist_engine is None:
        _blocklist_engine = _make_engine(settings.blocklist_database_url)
    return _blocklist_engine


def get_hosts_engine() -> Engine:
    global _hosts_engine
    if _hosts_engine is None:
        _hosts_engine = _make_engine(settings.localhosts_database_url)
    return _hosts_engine


# (declarative base, engine getter) for every database split out of the old mirenai.db.
_SPLIT_SCHEMAS: tuple[tuple[type[DeclarativeBase], Callable[[], Engine]], ...] = (
    (ConfigBase, get_config_engine),
    (StatsBase, get_stats_engine),
    (CacheBase, get_cache_engine),
    (QueryLogBase, get_querylog_engine),
)


def get_session_factory() -> sessionmaker[Session]:
    """Factory whose per-table binds route each table to its own split database."""
    global _session_factory
    if _session_factory is None:
        binds: dict[Any, Engine] = {}
        for base, get_engine_ in _SPLIT_SCHEMAS:
            engine = get_engine_()
            for table in base.metadata.sorted_tables:
                binds[table] = engine
        _session_factory = sessionmaker(binds=binds, expire_on_commit=False, future=True)
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


def create_all_schemas() -> None:
    """Create every split database's tables in its own engine."""
    for base, get_engine_ in _SPLIT_SCHEMAS:
        base.metadata.create_all(get_engine_())


def reset_engines() -> None:
    """Drop cached engines and session factories so they rebuild (test helper)."""
    global _config_engine, _stats_engine, _cache_engine, _querylog_engine
    global _blocklist_engine, _hosts_engine
    global _session_factory, _blocklist_session_factory, _hosts_session_factory
    _config_engine = _stats_engine = _cache_engine = _querylog_engine = None
    _blocklist_engine = _hosts_engine = None
    _session_factory = _blocklist_session_factory = _hosts_session_factory = None


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
    engines: dict[str, Callable[[], Engine]] = {
        "config": get_config_engine,
        "stats": get_stats_engine,
        "cache": get_cache_engine,
        "querylog": get_querylog_engine,
        "blocklist": get_blocklist_engine,
        "hosts": get_hosts_engine,
    }
    for name, get_engine_ in engines.items():
        try:
            with get_engine_().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return False, f"{name} database check failed: {type(exc).__name__}"
    return True, "ok"


def init_db() -> None:
    import mirenai.repository.models  # noqa: F401  (register tables on the bases' metadata)

    create_all_schemas()
    BlocklistBase.metadata.create_all(get_blocklist_engine())
    HostsBase.metadata.create_all(get_hosts_engine())

    _migrate_legacy_database()

    _ensure_hosts_icon_column()
    _ensure_hosts_mac_column()
    _ensure_hosts_excluded_column()
    _ensure_hosts_flag_new_domains_column()
    _ensure_blocklist_domain_first_seen_column()

    from mirenai.repository.blocklists import ensure_default_blocklist
    from mirenai.repository.upstreams import ensure_default_upstream

    ensure_default_blocklist()
    ensure_default_upstream()


def _migrate_legacy_database() -> None:
    """Copy data from the pre-split single database into the new per-purpose files.

    One-time and idempotent: runs only while the legacy ``mirenai.db`` still exists,
    copies each table into the matching new database when that target table is still
    empty, then renames the legacy file aside so it never runs again. Columns are
    matched by name (the legacy schema may order them differently or lack newer
    columns); a table whose required columns are absent in the legacy copy is skipped.
    """
    path = make_url(settings.legacy_database_url).database
    if not path or path == ":memory:" or not os.path.exists(path):
        return

    legacy_engine = _make_engine(settings.legacy_database_url)
    try:
        legacy_tables = _legacy_table_names(legacy_engine)
        for base, get_engine_ in _SPLIT_SCHEMAS:
            engine = get_engine_()
            for table in base.metadata.sorted_tables:
                if table.name in legacy_tables:
                    _copy_legacy_table(legacy_engine, engine, table)
    finally:
        legacy_engine.dispose()

    try:
        os.rename(path, f"{path}.migrated")
    except OSError:
        log.warning("could not rename migrated legacy database %s", path)
    log.info("migrated legacy database %s into the split databases", path)


def _legacy_table_names(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        return {row[0] for row in rows}


def _legacy_columns(engine: Engine, table_name: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({_quote(table_name)})"))
        return {row[1] for row in rows}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _copy_legacy_table(legacy_engine: Engine, engine: Engine, table: Table) -> None:
    """Copy one table's rows from the legacy database into an empty target table."""
    with engine.begin() as dst:
        already = dst.execute(select(func.count()).select_from(table)).scalar_one()
        if already:
            return
        legacy_cols = _legacy_columns(legacy_engine, table.name)
        usable = [col.name for col in table.columns if col.name in legacy_cols]
        missing_required = [
            col.name
            for col in table.columns
            if col.name not in legacy_cols
            and not col.nullable
            and col.default is None
            and col.server_default is None
        ]
        if not usable or missing_required:
            if missing_required:
                log.warning(
                    "skipping legacy table %s: missing required column(s) %s",
                    table.name,
                    ", ".join(missing_required),
                )
            return
        # Select via the typed columns so e.g. DateTime values deserialize to datetimes
        # rather than the raw strings a text() query yields (which fail on re-insert).
        with legacy_engine.connect() as src:
            rows = src.execute(select(*(table.c[name] for name in usable))).mappings().all()
        if rows:
            dst.execute(table.insert(), [dict(row) for row in rows])



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


def _ensure_blocklist_domain_first_seen_column() -> None:
    """Add ``blocklist_domains.first_seen`` to a blocklist database created before it.

    ``create_all`` never alters existing tables and this project has no migration
    tool, so a pre-existing ``blocklist_domains`` table needs a one-off ``ALTER``.
    SQLite forbids a non-constant default (e.g. ``datetime('now')``) on ``ADD
    COLUMN``, so the column is added nullable and existing rows are backfilled to
    now; ``replace_domains`` always supplies ``first_seen`` on insert. The matching
    index is created separately. Idempotent and a no-op on fresh databases (where
    ``create_all`` already added both).
    """
    with get_blocklist_engine().begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(blocklist_domains)"))}
        if columns and "first_seen" not in columns:
            conn.execute(text("ALTER TABLE blocklist_domains ADD COLUMN first_seen DATETIME"))
            conn.execute(
                text(
                    "UPDATE blocklist_domains SET first_seen = datetime('now', 'localtime') "
                    "WHERE first_seen IS NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_blocklist_domains_first_seen "
                    "ON blocklist_domains (first_seen)"
                )
            )
