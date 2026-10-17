import ssl
import struct
from unittest.mock import MagicMock

import pytest
from dnslib import DNSRecord

from mirenai.domain import upstream_transport as transport
from mirenai.domain.state import UpstreamServer


def _server(tls_server_name: str | None = "cloudflare-dns.com") -> UpstreamServer:
    return UpstreamServer(
        "1.1.1.1", 853, "udp", 100, use_tls=True,
        tls_server_name=tls_server_name,
    )


@pytest.fixture()
def tls_connection(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    connection = MagicMock()
    secured = MagicMock()
    context = MagicMock()
    connection.__enter__.return_value = connection
    secured.__enter__.return_value = secured
    context.wrap_socket.return_value = secured
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(transport.socket, "create_connection", connect)
    monkeypatch.setattr(transport, "_tls_context", lambda: context)
    return connect, context, secured


def test_tls_context_requires_certificate_and_hostname_verification() -> None:
    context = transport._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_tls_framing_sni_and_fragmented_response(
    tls_connection: tuple[MagicMock, MagicMock, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, context, secured = tls_connection
    request = DNSRecord.question("example.com")
    reply = request.reply().pack()
    frame = struct.pack("!H", len(reply)) + reply
    secured.recv.side_effect = [bytes([byte]) for byte in frame]
    plaintext = MagicMock(side_effect=AssertionError("plaintext transport was used"))
    monkeypatch.setattr(DNSRecord, "send", plaintext)

    result = transport.query_upstream(request, _server(), 5)

    assert result.pack() == reply
    assert connect.call_args.args == (("1.1.1.1", 853),)
    assert 0 < connect.call_args.kwargs["timeout"] <= 5
    context.wrap_socket.assert_called_once_with(
        connect.return_value, server_hostname="cloudflare-dns.com"
    )
    packet = request.pack()
    secured.sendall.assert_called_once_with(struct.pack("!H", len(packet)) + packet)
    secured.__exit__.assert_called_once()
    connect.return_value.__exit__.assert_called_once()
    plaintext.assert_not_called()


@pytest.mark.parametrize("failure", [
    ssl.SSLCertVerificationError("certificate mismatch"),
    TimeoutError("timed out"),
    OSError("connection refused"),
])
def test_tls_failure_never_downgrades_to_plaintext(
    tls_connection: tuple[MagicMock, MagicMock, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    _connect, context, _secured = tls_connection
    context.wrap_socket.side_effect = failure
    plaintext = MagicMock()
    monkeypatch.setattr(DNSRecord, "send", plaintext)
    with pytest.raises(type(failure)):
        transport.query_upstream(DNSRecord.question("example.com"), _server(), 5)
    plaintext.assert_not_called()


@pytest.mark.parametrize("chunks", [[b""], [b"\x00", b""], [b"\x00\x10", b"\x00", b""]])
def test_tls_rejects_incomplete_frames(
    tls_connection: tuple[MagicMock, MagicMock, MagicMock], chunks: list[bytes],
) -> None:
    tls_connection[2].recv.side_effect = chunks
    with pytest.raises(ConnectionError, match="before the full DNS reply"):
        transport.query_upstream(DNSRecord.question("example.com"), _server(), 5)


@pytest.mark.parametrize("mismatch", ["id", "question", "qr"])
def test_tls_rejects_mismatched_replies(
    tls_connection: tuple[MagicMock, MagicMock, MagicMock], mismatch: str,
) -> None:
    request = DNSRecord.question("example.com")
    reply = request.reply()
    if mismatch == "id":
        reply.header.id ^= 1
    elif mismatch == "qr":
        reply.header.qr = 0
    else:
        reply.questions = DNSRecord.question("other.example").questions
    packet = reply.pack()
    tls_connection[2].recv.side_effect = [struct.pack("!H", len(packet)), packet]
    with pytest.raises(ValueError, match="does not match"):
        transport.query_upstream(request, _server(), 5)


def test_tls_requires_server_name_before_connecting(
    tls_connection: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    with pytest.raises(ValueError, match="tls_server_name is required"):
        transport.query_upstream(DNSRecord.question("example.com"), _server(None), 5)
    tls_connection[0].assert_not_called()


def test_tls_enforces_total_timeout(
    tls_connection: tuple[MagicMock, MagicMock, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MagicMock(side_effect=[0.0, 0.1, 0.2, 0.3, 6.0])
    monkeypatch.setattr(transport.time, "monotonic", clock)
    with pytest.raises(TimeoutError, match="TLS query timed out"):
        transport.query_upstream(DNSRecord.question("example.com"), _server(), 5)
