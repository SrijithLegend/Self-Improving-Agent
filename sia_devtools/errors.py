"""One exception family for the SDK, the local store and the hosted API. `status` is the HTTP code."""


class SIAError(Exception):
    status = 500


class ValidationError(SIAError):
    status = 400


class AuthError(SIAError):
    status = 401


class QuotaExceeded(SIAError):
    status = 402


class PermissionDenied(SIAError):
    status = 403


class NotFound(SIAError):
    status = 404


class Conflict(SIAError):
    status = 409


class PayloadTooLarge(SIAError):
    status = 413


class RateLimited(SIAError):
    status = 429


class ServerError(SIAError):
    """The server or network failed after retries. Safe to retry later."""
    status = 502


BY_STATUS = {c.status: c for c in (ValidationError, AuthError, QuotaExceeded, PermissionDenied, NotFound, Conflict,
                                    PayloadTooLarge, RateLimited)}
