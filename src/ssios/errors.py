"""Stable error codes returned by the control plane."""


class SsiosError(Exception):
    def __init__(self, code: str, detail: str, status: int = 400, reason: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.reason = reason


class FlowInterrupt(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind
