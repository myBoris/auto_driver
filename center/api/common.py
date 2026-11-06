"""HTTP 请求解码与帧响应头；不包含业务决策。"""

from ..domain.errors import APIError


async def read_object(request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeError) as exc:
        raise APIError(400, "request body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise APIError(400, "request body must be an object")
    return data


def frame_headers(frame):
    return {"Cache-Control": "no-store", "X-Frame-Sequence": str(frame.sequence),
            "X-Frame-Timestamp": str(frame.captured_at), "X-Frame-Received-At": str(frame.received_at)}
