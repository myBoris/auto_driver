"""媒体领域规则：终端路径、发布地址及 Docker NAT 后的 ICE 地址转换。

媒体服务生成的容器网桥 host 地址通常不能从局域网直达，因此按浏览器
当前访问中心的主机地址转换候选；ICE/DTLS 凭据及中继候选保持完整。
"""

import ipaddress
import re
from urllib.parse import quote, urlsplit

from .protocol import TERMINAL_ID


def media_path(terminal_id: str) -> str:
    if not isinstance(terminal_id, str) or not TERMINAL_ID.fullmatch(terminal_id):
        raise ValueError("invalid terminal media path")
    return f"terminal/{terminal_id}"


def terminal_from_media_path(path: str) -> str | None:
    if not isinstance(path, str) or not path.startswith("terminal/"):
        return None
    terminal_id = path.removeprefix("terminal/")
    return terminal_id if TERMINAL_ID.fullmatch(terminal_id) else None


def request_hostname(authority: str) -> str:
    parsed = urlsplit("//" + authority)
    host = parsed.hostname
    if not host or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid center hostname")
    parsed.port  # 单独校验可选端口，避免把 IPv6 冒号误认为端口分隔符。
    if host.lower() == "localhost":
        return "127.0.0.1"
    if any(char.isspace() or char in "/?#@" for char in host):
        raise ValueError("invalid center hostname")
    return host


def publisher_url(authority: str, terminal_id: str, token: str, port: int) -> str:
    host = request_hostname(authority)
    if ":" in host:
        host = f"[{host}]"
    return f"rtsp://publisher:{quote(token, safe='')}@{host}:{port}/{media_path(terminal_id)}"


def rewrite_ice_candidates(sdp: str, host: str, port: int) -> str:
    """按当前访问地址重写 host 候选，并保留 STUN、中继和认证信息。

    Compose 把 UDP/TCP 监听器映射到同一个媒体端口。上游完全没有 host
    候选时才补充两种协议的候选，避免依赖 Docker 内部接口枚举结果。
    """
    host = host.strip("[]")
    if host.lower() == "localhost":
        host = "127.0.0.1"
    if not host or any(char.isspace() or char in "/?#@" for char in host):
        raise ValueError("invalid ICE hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise ValueError("invalid ICE hostname") from None
    else:
        if address.is_unspecified:
            raise ValueError("ICE hostname must be a reachable address")
    lines = sdp.replace("\r\n", "\n").splitlines()
    rewritten = []
    seen = set()
    candidate_count = 0
    for line in lines:
        if line.startswith("a=candidate:"):
            fields = line.split()
            if len(fields) < 8 or fields[6] != "typ":
                raise ValueError("invalid SDP candidate")
            if fields[7].lower() == "host":
                fields[4] = host
                fields[5] = str(port)
                key = (fields[1], fields[2].lower(), host, port)
                if key in seen:
                    continue
                seen.add(key)
                candidate_count += 1
                line = " ".join(fields)
        rewritten.append(line)
    if not candidate_count:
        candidates = [
            f"a=candidate:90001 1 udp 2130706431 {host} {port} typ host",
            f"a=candidate:90002 1 tcp 2130706430 {host} {port} typ host tcptype passive",
        ]
        index = next((i for i, line in enumerate(rewritten) if line == "a=end-of-candidates"), len(rewritten))
        rewritten[index:index] = candidates
    return "\r\n".join(rewritten) + "\r\n"
