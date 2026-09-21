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
