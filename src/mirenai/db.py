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

    from mirenai.repository.upstreams import ensure_default_upstream

    ensure_default_upstream()
