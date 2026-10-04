"""Runtime state shared by the DNS server.

Policies, upstream resolvers, and tunable settings all live in the database and
are edited through the API/UI. The DNS server keeps an in-memory snapshot and
refreshes it on a background timer so operator changes take effect without a
restart (within ``refresh_seconds``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from mirenai.domain.localzones import LocalRecord, LocalRecords
from mirenai.domain.network import NetworkFilter
from mirenai.domain.policy import PolicyRule
from mirenai.logging import get_logger

log = get_logger("dns.state")


@dataclass(frozen=True)
class UpstreamServer:
    address: str
    port: int
    protocol: str
    priority: int


@dataclass(frozen=True)
class RuntimeSettings:
    cache_enabled: bool = True
    cache_max_ttl: int = 3600
    cache_min_ttl: int = 0
    cache_max_entries: int = 10000
    forward_timeout: float = 5.0
    default_action: str = "forward"
    refresh_seconds: int = 10
    query_flush_seconds: int = 5
    log_queries: bool = True
    ipv6_enabled: bool = True
    drop_private_ptr: bool = True


SettingsLoader = Callable[[], RuntimeSettings]
PolicyLoader = Callable[[], list[PolicyRule]]
UpstreamLoader = Callable[[], list[UpstreamServer]]
BlocklistLoader = Callable[[], frozenset[str]]
BlocklistExcludedLoader = Callable[[], frozenset[str]]
TrustedNetworkLoader = Callable[[], list[str]]
LocalRecordsLoader = Callable[[], list[LocalRecord]]


class RuntimeState:
    def __init__(
        self,
        load_settings: SettingsLoader,
        load_policies: PolicyLoader,
        load_upstreams: UpstreamLoader,
        load_blocklist: BlocklistLoader,
        load_blocklist_excluded: BlocklistExcludedLoader,
        load_trusted_networks: TrustedNetworkLoader,
        load_local_records: LocalRecordsLoader,
    ) -> None:
        self._load_settings = load_settings
        self._load_policies = load_policies
        self._load_upstreams = load_upstreams
        self._load_blocklist = load_blocklist
        self._load_blocklist_excluded = load_blocklist_excluded
        self._load_trusted_networks = load_trusted_networks
        self._load_local_records = load_local_records
        self._lock = threading.RLock()
        self._settings = RuntimeSettings()
        self._policies: list[PolicyRule] = []
        self._upstreams: list[UpstreamServer] = []
        self._blocklist: frozenset[str] = frozenset()
        self._blocklist_excluded: frozenset[str] = frozenset()
        self._network_filter = NetworkFilter()
        self._local_records = LocalRecords([])
        self._stop = threading.Event()
        self.reload()

    def reload(self) -> None:
        new_settings = self._load_settings()
        new_policies = self._load_policies()
        new_upstreams = self._load_upstreams()
        new_blocklist = self._load_blocklist()
        new_blocklist_excluded = self._load_blocklist_excluded()
        new_network_filter = NetworkFilter.from_cidrs(self._load_trusted_networks())
        new_local_records = LocalRecords(self._load_local_records())
        with self._lock:
            self._settings = new_settings
            self._policies = new_policies
            self._upstreams = new_upstreams
            self._blocklist = new_blocklist
            self._blocklist_excluded = new_blocklist_excluded
            self._network_filter = new_network_filter
            self._local_records = new_local_records

    @property
    def settings(self) -> RuntimeSettings:
        with self._lock:
            return self._settings

    @property
    def policies(self) -> list[PolicyRule]:
        with self._lock:
            return self._policies

    @property
    def upstreams(self) -> list[UpstreamServer]:
        with self._lock:
            return self._upstreams

    @property
    def blocklist(self) -> frozenset[str]:
        with self._lock:
            return self._blocklist

    @property
    def blocklist_excluded(self) -> frozenset[str]:
        with self._lock:
            return self._blocklist_excluded

    @property
    def network_filter(self) -> NetworkFilter:
        with self._lock:
            return self._network_filter

    @property
    def local_records(self) -> LocalRecords:
        with self._lock:
            return self._local_records

    def start_refresh(self) -> None:
        thread = threading.Thread(target=self._refresh_loop, name="config-refresh", daemon=True)
        thread.start()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self.settings.refresh_seconds):
            try:
                self.reload()
            except Exception:
                log.exception("config reload failed")

    def stop(self) -> None:
        self._stop.set()
