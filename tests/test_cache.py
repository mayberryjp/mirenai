from mirenai.domain.cache import TTLCache


def test_set_and_get() -> None:
    cache: TTLCache[str] = TTLCache(10)
    cache.set("k", "v", 100)
    entry = cache.get("k")
    assert entry is not None
    assert entry.value == "v"


def test_zero_ttl_expires_immediately() -> None:
    cache: TTLCache[str] = TTLCache(10)
    cache.set("k", "v", 0)
    assert cache.get("k") is None


def test_lru_eviction_by_recency() -> None:
    cache: TTLCache[int] = TTLCache(2)
    cache.set("a", 1, 100)
    cache.set("b", 2, 100)
    # Touch "a" so "b" becomes least-recently-used.
    assert cache.get("a") is not None
    cache.set("c", 3, 100)
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None


def test_len_and_clear() -> None:
    cache: TTLCache[int] = TTLCache(10)
    cache.set("a", 1, 100)
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


def test_capacity_reports_max_entries() -> None:
    assert TTLCache(42).capacity == 42
    assert TTLCache(0).capacity == 1  # clamped to a minimum of 1


def test_snapshot_returns_live_entries_only() -> None:
    cache: TTLCache[str] = TTLCache(10)
    cache.set("a", "x", 100)
    cache.set("b", "y", 0)  # expires immediately
    snap = {key: (value, remaining, ttl) for key, value, remaining, ttl in cache.snapshot()}
    assert "b" not in snap
    value, remaining, ttl = snap["a"]
    assert value == "x"
    assert ttl == 100
    assert 0 < remaining <= 100

