class ZeroDeltaError(Exception):
    """Base class for all Zero Delta provider errors."""


class ZeroDeltaAccessError(ZeroDeltaError):
    """Database authentication or table permission failure (not an HTTP session error)."""


class ZeroDeltaConfigError(ZeroDeltaError):
    """Required configuration is missing or invalid."""


class ZeroDeltaAuthError(ZeroDeltaError):
    """401 / 403: the session is missing, expired or not allowed."""

    def __init__(self, status_code: int):
        super().__init__(f"Zero Delta rejected the session (HTTP {status_code})")
        self.status_code = status_code


class ZeroDeltaRateLimitError(ZeroDeltaError):
    """429: not retried, so the provider never works around a rate limit."""


class ZeroDeltaServerError(ZeroDeltaError):
    """5xx after retries were used up."""

    def __init__(self, status_code: int):
        super().__init__(f"Zero Delta server error (HTTP {status_code})")
        self.status_code = status_code


class ZeroDeltaTimeoutError(ZeroDeltaError):
    """Request timed out or the connection failed after retries."""


class ZeroDeltaParseError(ZeroDeltaError):
    """The response is not valid JSON or lacks the structure the provider needs."""
