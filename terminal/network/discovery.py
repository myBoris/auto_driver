"""局域网 UDP 发现：随机 nonce 匹配回应，不广播口令，随后由 WebSocket 认证。"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from urllib.parse import urlparse

SERVICE = "auto-driver-center"


def validate_center_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("center URL must be http://host:port or https://host:port")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("center URL must not contain a path, query, or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid center port") from exc
    return value.rstrip("/")


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, nonce: str, result: asyncio.Future[str]) -> None:
        self.nonce = nonce
        self.result = result

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.result.done() or len(data) > 4096:
            return
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                return
            if (value.get("service"), value.get("type"), value.get("protocol"), value.get("nonce")) != (SERVICE, "offer", 1, self.nonce):
                return
            port = value.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                return
            url = validate_center_url(value.get("url") or f"http://{addr[0]}:{port}")
            self.result.set_result(url)
        except (ValueError, TypeError, AttributeError):
            return


async def discover_center(port: int = 9999, address: str = "255.255.255.255", timeout: float = 3) -> str:
    loop = asyncio.get_running_loop()
    nonce = secrets.token_hex(16)
    result: asyncio.Future[str] = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(nonce, result), local_addr=("0.0.0.0", 0),
        family=socket.AF_INET, allow_broadcast=True,
    )
    try:
        packet = json.dumps({"service": SERVICE, "type": "discover", "protocol": 1, "nonce": nonce}).encode()
        for target in dict.fromkeys((address, "127.0.0.1")):
            try:
                transport.sendto(packet, (target, port))
            except OSError:
                pass
        return await asyncio.wait_for(result, timeout)
    finally:
        transport.close()
