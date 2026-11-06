"""局域网 UDP 探测，只公布服务地址，不在广播中传递口令。"""

import asyncio
import json
import logging

from .config import CenterConfig
from .protocol import PROTOCOL, SERVICE

log = logging.getLogger(__name__)


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, config: CenterConfig):
        self.config = config
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if len(data) > 1024:
            return
        try:
            request = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if not isinstance(request, dict):
            return
        nonce = request.get("nonce")
        if (request.get("type") != "discover" or request.get("service") != SERVICE
                or request.get("protocol") != PROTOCOL
                or not isinstance(nonce, str) or not 1 <= len(nonce) <= 128):
            return
        response = {
            "type": "offer", "service": SERVICE, "protocol": PROTOCOL,
            "nonce": nonce, "port": self.config.port,
        }
        if self.config.advertise_url:
            response["url"] = self.config.advertise_url.rstrip("/")
        self.transport.sendto(json.dumps(response).encode(), addr)


async def start_discovery(config: CenterConfig):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(config), local_addr=("0.0.0.0", config.discovery_port),
        allow_broadcast=True,
    )
    log.info("Discovery listening on UDP %s", config.discovery_port)
    return transport
