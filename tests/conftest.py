import socket

import pytest

from invest_bot import prices

_connect = socket.socket.connect
_getaddrinfo = socket.getaddrinfo
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class _NoNetwork:
    """Stands in for yfinance (it uses libcurl, below the socket guard)."""

    def __getattr__(self, name):
        raise RuntimeError("network disabled in tests: yfinance." + name)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach MNB, yfinance or K&H. Loopback stays open (asyncio's self-pipe uses it on Windows)."""
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] in LOOPBACK:
            return _connect(sock, address)
        raise RuntimeError(f"network disabled in tests: {address!r}")

    def getaddrinfo(host, *a, **k):
        if host in LOOPBACK:
            return _getaddrinfo(host, *a, **k)
        raise RuntimeError(f"network disabled in tests: DNS {host!r}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(prices, "yf", _NoNetwork())
