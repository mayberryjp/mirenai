"""Shared plain DNS and authenticated DNS-over-TLS upstream transport."""

from __future__ import annotations

import socket
import ssl
import struct
import time
from functools import lru_cache

from dnslib import DNSRecord

from mirenai.domain.state import UpstreamServer


@lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("upstream TLS query timed out")
    return remaining


def _read_exact(connection: ssl.SSLSocket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("upstream closed the TLS connection before the full DNS reply")
        data.extend(chunk)
    return bytes(data)


def query_upstream(request: DNSRecord, server: UpstreamServer, timeout: float) -> DNSRecord:
    if server.use_tls:
        if not server.tls_server_name:
            raise ValueError("tls_server_name is required when use_tls is true")
        context = _tls_context()
        deadline = time.monotonic() + timeout
        with socket.create_connection(
            (server.address, server.port), timeout=_remaining(deadline)
        ) as connection:
            connection.settimeout(_remaining(deadline))
            with context.wrap_socket(
                connection, server_hostname=server.tls_server_name
            ) as secured:
                packet = request.pack()
                secured.settimeout(_remaining(deadline))
                secured.sendall(struct.pack("!H", len(packet)) + packet)
                length = struct.unpack("!H", _read_exact(secured, 2, deadline))[0]
                reply = DNSRecord.parse(_read_exact(secured, length, deadline))
                if (
                    not reply.header.qr
                    or reply.header.id != request.header.id
                    or reply.questions != request.questions
                ):
                    raise ValueError("upstream TLS reply does not match the DNS query")
                return reply

    use_tcp = server.protocol == "tcp"
    reply = DNSRecord.parse(
        request.send(server.address, server.port, tcp=use_tcp, timeout=timeout)
    )
    if reply.header.tc and not use_tcp:
        reply = DNSRecord.parse(
            request.send(server.address, server.port, tcp=True, timeout=timeout)
        )
    return reply
