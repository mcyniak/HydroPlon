"""Hierarchia błędów źródeł danych i warstwy API.

Rozróżnienie wymagane przez §31.2: błąd wejścia, nieznaleziony obiekt, timeout,
błąd usługi zewnętrznej, niepoprawny format odpowiedzi, adapter niewdrożony.
"""
from __future__ import annotations


class HydroPlonError(Exception):
    http_status = 500
    error_code = "internal_error"

    def __init__(self, message_pl: str, *, details: dict | None = None):
        super().__init__(message_pl)
        self.message_pl = message_pl
        self.details = details or {}


class InputValidationError(HydroPlonError):
    http_status = 400
    error_code = "invalid_input"


class NotFoundError(HydroPlonError):
    http_status = 404
    error_code = "not_found"


class AmbiguousResultError(HydroPlonError):
    http_status = 409
    error_code = "ambiguous_result"


class ConsistencyError(HydroPlonError):
    """Próba połączenia wyników z różnych analiz / działek / rankingów."""
    http_status = 409
    error_code = "inconsistent_identifiers"


class UnprocessableError(HydroPlonError):
    http_status = 422
    error_code = "unprocessable"


class SourceError(HydroPlonError):
    """Błąd zewnętrznego źródła danych."""
    http_status = 502
    error_code = "upstream_error"


class SourceTimeoutError(SourceError):
    http_status = 504
    error_code = "upstream_timeout"


class SourceRateLimitedError(SourceError):
    """Usługa odrzuciła klienta mechanizmem ochrony (limit zapytań na IP)."""
    http_status = 503
    error_code = "upstream_rate_limited"


class SourceFormatError(SourceError):
    http_status = 502
    error_code = "upstream_bad_format"


class AdapterNotImplementedError(SourceError):
    http_status = 503
    error_code = "adapter_not_implemented"
