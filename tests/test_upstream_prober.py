from types import SimpleNamespace

import pytest

from mirenai.domain.state import UpstreamServer
from mirenai.domain.upstream_probe import ProbeError
from mirenai.domain.upstreamstats import UpstreamRttAgg, UpstreamRttBuffer
from mirenai.workers import dns_server


def _server(address: str) -> UpstreamServer:
    return UpstreamServer(address=address, port=53, protocol="udp", priority=100)


def _state(*addresses: str, timeout: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(
        upstreams=[_server(address) for address in addresses],
        settings=SimpleNamespace(forward_timeout=timeout),
    )


def test_gap_fill_probes_each_uncovered_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[UpstreamRttAgg] = []
    rtt = UpstreamRttBuffer(flush=captured.extend, flush_seconds=3600)
    timings = {"1.1.1.1": 12.0, "8.8.8.8": 20.0}
    monkeypatch.setattr(
        dns_server, "probe_upstream", lambda server, timeout: timings[server.address]
    )

    dns_server._probe_rtt_gaps(_state("1.1.1.1", "8.8.8.8"), rtt)
    rtt.flush()

    by_addr = {agg.address: agg for agg in captured}
    assert by_addr["1.1.1.1"].samples == 1
    assert by_addr["1.1.1.1"].total_ms == 12.0
    assert by_addr["8.8.8.8"].total_ms == 20.0


def test_gap_fill_skips_upstream_measured_this_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[UpstreamRttAgg] = []
    rtt = UpstreamRttBuffer(flush=captured.extend, flush_seconds=3600)
    rtt.add("1.1.1.1", 9.0)  # organic forward already measured this hour

    probed: list[str] = []

    def fake_probe(server: UpstreamServer, timeout: float) -> float:
        probed.append(server.address)
        return 20.0

    monkeypatch.setattr(dns_server, "probe_upstream", fake_probe)

    dns_server._probe_rtt_gaps(_state("1.1.1.1", "8.8.8.8"), rtt)

    assert probed == ["8.8.8.8"]


def test_gap_fill_attempts_unreachable_upstream_once(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[UpstreamRttAgg] = []
    rtt = UpstreamRttBuffer(flush=captured.extend, flush_seconds=3600)

    attempts: list[str] = []

    def fake_probe(server: UpstreamServer, timeout: float) -> float:
        attempts.append(server.address)
        if server.address == "8.8.8.8":
            raise ProbeError("unreachable")
        return 12.0

    monkeypatch.setattr(dns_server, "probe_upstream", fake_probe)

    state = _state("1.1.1.1", "8.8.8.8")
    dns_server._probe_rtt_gaps(state, rtt)
    dns_server._probe_rtt_gaps(state, rtt)  # same hour: nothing left to probe
    rtt.flush()

    assert [agg.address for agg in captured] == ["1.1.1.1"]
    assert attempts == ["1.1.1.1", "8.8.8.8"]
