from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, insert, select, text

from mirenai import config, db
from mirenai.repository.models import (
    ClientHourlyStat,
    Policy,
    QueryLog,
    UncacheableResponse,
)


def _point_split_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("config", "stats", "cache", "querylog"):
        monkeypatch.setattr(
            config.settings, f"{name}_database_url", f"sqlite:///{tmp_path / f'{name}.db'}"
        )


def _legacy_engine_with_all_tables(path: Path) -> Engine:
    """Build a pre-split database: every split table living in one file."""
    engine = db._make_engine(f"sqlite:///{path}")
    for base, _get in db._SPLIT_SCHEMAS:
        base.metadata.create_all(engine)
    return engine


def test_migration_copies_rows_and_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "legacy_database_url", f"sqlite:///{legacy_path}")
    _point_split_dbs(tmp_path, monkeypatch)

    legacy_engine = _legacy_engine_with_all_tables(legacy_path)
    with legacy_engine.begin() as conn:
        conn.execute(insert(Policy), [{"client": "10.0.0.1", "domain": "a.test", "action": "deny"}])
        conn.execute(
            insert(ClientHourlyStat),
            [{"hour_start": datetime(2026, 1, 1), "client": "10.0.0.1", "total": 5}],
        )
        conn.execute(
            insert(QueryLog),
            [{"client": "10.0.0.1", "domain": "a.test", "qtype": "A", "count": 3}],
        )
    legacy_engine.dispose()

    db.create_all_schemas()
    db._migrate_legacy_database()

    with db.get_config_engine().connect() as conn:
        action = conn.execute(select(Policy.action)).scalar_one()
    with db.get_stats_engine().connect() as conn:
        total = conn.execute(select(ClientHourlyStat.total)).scalar_one()
    with db.get_querylog_engine().connect() as conn:
        count = conn.execute(select(QueryLog.count)).scalar_one()
    assert (action, total, count) == ("deny", 5, 3)
    assert not legacy_path.exists()
    assert Path(f"{legacy_path}.migrated").exists()


def test_migration_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "legacy_database_url", f"sqlite:///{legacy_path}")
    _point_split_dbs(tmp_path, monkeypatch)

    legacy_engine = _legacy_engine_with_all_tables(legacy_path)
    with legacy_engine.begin() as conn:
        conn.execute(
            insert(ClientHourlyStat),
            [{"hour_start": datetime(2026, 1, 1), "client": "10.0.0.1", "total": 5}],
        )
    legacy_engine.dispose()

    db.create_all_schemas()
    db._migrate_legacy_database()
    db._migrate_legacy_database()  # second run no-ops: the legacy file was renamed aside

    with db.get_stats_engine().connect() as conn:
        rows = conn.execute(select(func.count()).select_from(ClientHourlyStat)).scalar_one()
    assert rows == 1


def test_migration_skips_table_missing_required_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_path = tmp_path / "mirenai.db"
    monkeypatch.setattr(config.settings, "legacy_database_url", f"sqlite:///{legacy_path}")
    _point_split_dbs(tmp_path, monkeypatch)

    legacy_engine = _legacy_engine_with_all_tables(legacy_path)
    with legacy_engine.begin() as conn:
        # Simulate the pre-client uncacheable_responses schema (no NOT NULL ``client``).
        conn.execute(text("DROP TABLE uncacheable_responses"))
        conn.execute(
            text(
                "CREATE TABLE uncacheable_responses ("
                "id INTEGER PRIMARY KEY, domain VARCHAR(255) NOT NULL, "
                "qtype VARCHAR(16) NOT NULL, reason VARCHAR(32) NOT NULL, "
                "last_ttl INTEGER, hits BIGINT NOT NULL DEFAULT 0, "
                "first_seen DATETIME, last_seen DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO uncacheable_responses (domain, qtype, reason, hits) "
                "VALUES ('x.test', 'A', 'nodata', 2)"
            )
        )
        conn.execute(
            insert(Policy), [{"client": "10.0.0.2", "domain": "b.test", "action": "forward"}]
        )
    legacy_engine.dispose()

    db.create_all_schemas()
    db._migrate_legacy_database()

    with db.get_stats_engine().connect() as conn:
        uncacheable = conn.execute(select(func.count()).select_from(UncacheableResponse)).scalar_one()
    with db.get_config_engine().connect() as conn:
        policies = conn.execute(select(func.count()).select_from(Policy)).scalar_one()
    assert uncacheable == 0  # skipped: legacy copy lacked the required ``client`` column
    assert policies == 1  # unrelated tables still migrate


def test_migration_noop_without_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.settings, "legacy_database_url", f"sqlite:///{tmp_path / 'absent.db'}")
    _point_split_dbs(tmp_path, monkeypatch)
    db.create_all_schemas()
    db._migrate_legacy_database()  # must not raise when there is nothing to migrate
