"""可解释的拒绝响应。"""


class Reject(Exception):
    """业务拒绝：携带错误码、人类可读说明和字段级明细，由 API 层翻译成响应体。"""

    def __init__(self, code, message, details=None, status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = list(details or [])
        self.status = status

    def body(self):
        return {"error": self.code, "message": self.message, "details": self.details}
