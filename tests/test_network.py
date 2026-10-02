import pytest

from mirenai.domain.network import NetworkFilter
from mirenai.workers.dns_server import ScreeningDNSHandler


def test_empty_filter_trusts_all() -> None:
    f = NetworkFilter.from_cidrs([])
    assert f.is_trusted("1.2.3.4") is True
    assert f.is_trusted("::1") is True


def test_filter_matches_configured_subnet() -> None:
    f = NetworkFilter.from_cidrs(["10.2.10.0/24"])
    assert f.is_trusted("10.2.10.1") is True
    assert f.is_trusted("10.2.10.255") is True
    assert f.is_trusted("10.2.11.1") is False


def test_filter_supports_multiple_subnets() -> None:
    f = NetworkFilter.from_cidrs(["10.2.10.0/24", "192.168.0.0/16"])
    assert f.is_trusted("10.2.10.5") is True
    assert f.is_trusted("192.168.50.50") is True
    assert f.is_trusted("172.16.0.1") is False


def test_filter_ipv6_subnet() -> None:
    f = NetworkFilter.from_cidrs(["2001:db8::/32"])
    assert f.is_trusted("2001:db8::1") is True
    assert f.is_trusted("2001:dead::1") is False


def test_filter_v4_and_v6_do_not_cross_match() -> None:
    f = NetworkFilter.from_cidrs(["0.0.0.0/0"])
    assert f.is_trusted("1.2.3.4") is True
    assert f.is_trusted("::1") is False  # a v6 address must not match a v4 network


def test_filter_invalid_address_is_not_trusted() -> None:
    f = NetworkFilter.from_cidrs(["10.0.0.0/8"])
    assert f.is_trusted("not-an-ip") is False


def test_from_cidrs_skips_invalid_entries() -> None:
    f = NetworkFilter.from_cidrs(["10.0.0.0/8", "garbage", "bad/cidr"])
    assert f.is_trusted("10.1.2.3") is True
    assert len(f.networks) == 1


class _FakeResolver:
    def __init__(self, trusted: bool) -> None:
        self._trusted = trusted
        self.recorded: list[str] = []

    def is_trusted(self, ip: str) -> bool:
        return self._trusted

    def record_foreign(self, ip: str) -> None:
        self.recorded.append(ip)


class _FakeServer:
    def __init__(self, resolver: object) -> None:
        self.resolver = resolver


def _make_handler(resolver: object, client_ip: str) -> ScreeningDNSHandler:
    handler = object.__new__(ScreeningDNSHandler)
    handler.server = _FakeServer(resolver)
    handler.client_address = (client_ip, 12345)
    return handler


def test_handler_drops_untrusted_client() -> None:
    resolver = _FakeResolver(trusted=False)
    handler = _make_handler(resolver, "192.168.1.9")
    # Returns without raising and without touching the socket (super().handle not called).
    handler.handle()
    assert resolver.recorded == ["192.168.1.9"]


def test_handler_passes_trusted_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import dnslib.server as server_mod

    called: list[bool] = []
    monkeypatch.setattr(server_mod.DNSHandler, "handle", lambda self: called.append(True))
    resolver = _FakeResolver(trusted=True)
    handler = _make_handler(resolver, "10.2.10.5")
    handler.handle()
    assert called == [True]
    assert resolver.recorded == []
