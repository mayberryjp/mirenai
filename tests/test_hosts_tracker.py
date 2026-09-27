from mirenai.domain.hosts import HostTracker


def _make(on_discover=None, known=None):
    flushed: list[dict[str, int]] = []
    tracker = HostTracker(
        flush=lambda counts: flushed.append(counts),
        load=lambda: set(known or set()),
        flush_seconds=1,
        refresh_seconds=1,
        on_discover=on_discover,
    )
    return tracker, flushed


def test_flush_reports_new_clients() -> None:
    discovered: list[set[str]] = []
    tracker, flushed = _make(on_discover=lambda ips: discovered.append(set(ips)))
    tracker.record("1.1.1.1")
    tracker.record("1.1.1.1")
    tracker.record("2.2.2.2")
    tracker.flush()
    assert flushed == [{"1.1.1.1": 2, "2.2.2.2": 1}]
    assert discovered == [{"1.1.1.1", "2.2.2.2"}]


def test_known_clients_not_reported_again() -> None:
    discovered: list[set[str]] = []
    tracker, _ = _make(on_discover=lambda ips: discovered.append(set(ips)))
    tracker.record("1.1.1.1")
    tracker.flush()
    discovered.clear()
    tracker.record("1.1.1.1")
    tracker.flush()
    assert discovered == []


def test_hosts_loaded_from_db_are_not_new() -> None:
    discovered: list[set[str]] = []
    tracker, _ = _make(on_discover=lambda ips: discovered.append(set(ips)), known={"9.9.9.9"})
    tracker.refresh()
    tracker.record("9.9.9.9")
    tracker.flush()
    assert discovered == []


def test_flush_without_discover_callback_is_safe() -> None:
    tracker, flushed = _make(on_discover=None)
    tracker.record("1.1.1.1")
    tracker.flush()
    assert flushed == [{"1.1.1.1": 1}]
