"""Stable error codes returned by the control plane."""


class SsiosError(Exception):
    def __init__(self, code: str, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class FlowInterrupt(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind
