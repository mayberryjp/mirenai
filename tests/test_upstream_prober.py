import pytest

from mirenai.domain.state import UpstreamServer
from mirenai.domain.upstream_probe import ProbeError
from mirenai.domain.upstreamstats import UpstreamRttAgg, UpstreamRttBuffer
from mirenai.workers import dns_server


def _server(address: str) -> UpstreamServer:
    return UpstreamServer(address=address, port=53, protocol="udp", priority=100)


def test_probe_once_records_each_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[UpstreamRttAgg] = []
    rtt = UpstreamRttBuffer(flush=captured.extend, flush_seconds=3600)
    timings = {"1.1.1.1": 12.0, "8.8.8.8": 20.0}
    monkeypatch.setattr(
        dns_server, "probe_upstream", lambda server, timeout: timings[server.address]
    )

    dns_server._probe_upstreams_once([_server("1.1.1.1"), _server("8.8.8.8")], 5.0, rtt)
    rtt.flush()

    by_addr = {agg.address: agg for agg in captured}
    assert by_addr["1.1.1.1"].samples == 1
    assert by_addr["1.1.1.1"].total_ms == 12.0
    assert by_addr["8.8.8.8"].total_ms == 20.0


def test_probe_once_skips_unreachable_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[UpstreamRttAgg] = []
    rtt = UpstreamRttBuffer(flush=captured.extend, flush_seconds=3600)

    def fake_probe(server: UpstreamServer, timeout: float) -> float:
        if server.address == "8.8.8.8":
            raise ProbeError("unreachable")
        return 12.0

    monkeypatch.setattr(dns_server, "probe_upstream", fake_probe)

    dns_server._probe_upstreams_once([_server("1.1.1.1"), _server("8.8.8.8")], 5.0, rtt)
    rtt.flush()

    assert [agg.address for agg in captured] == ["1.1.1.1"]
