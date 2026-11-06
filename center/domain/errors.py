"""业务错误携带稳定 HTTP 语义，统一由 API 中间件转换为 JSON。"""


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)
