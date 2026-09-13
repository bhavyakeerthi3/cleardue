from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

import httpx


class FailureKind(StrEnum):
    AUTH = "AUTH"
    PERMISSION = "PERMISSION"
    RATE_LIMIT = "RATE_LIMIT"
    TRANSIENT_READ = "TRANSIENT_READ"
    DEFINITE_REJECTION = "DEFINITE_REJECTION"
    UNCERTAIN_WRITE = "UNCERTAIN_WRITE"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    VERIFICATION_MISMATCH = "VERIFICATION_MISMATCH"


@dataclass
class ProviderError(RuntimeError):
    kind: FailureKind
    provider: str
    message: str
    retry_after: float | None = None

    def __str__(self) -> str:
        return f"{self.provider} {self.kind}: {self.message}"


class FaultInjector:
    def __init__(self) -> None:
        self._armed: set[tuple[str, str]] = set()

    def arm(self, provider: str, fault: str) -> None:
        self._armed.add((provider, fault))

    def consume(self, provider: str, fault: str) -> bool:
        key = (provider, fault)
        if key in self._armed:
            self._armed.remove(key)
            return True
        return False


FAULTS = FaultInjector()


def classify_response(response: httpx.Response, provider: str, write: bool) -> None:
    if response.status_code < 400:
        return
    message = response.text[:500]
    if response.status_code == 401:
        raise ProviderError(FailureKind.AUTH, provider, message)
    if response.status_code == 403:
        raise ProviderError(FailureKind.PERMISSION, provider, message)
    if response.status_code == 429:
        retry = response.headers.get("retry-after")
        raise ProviderError(
            FailureKind.RATE_LIMIT, provider, message, float(retry) if retry and retry.isdigit() else None
        )
    if response.status_code >= 500:
        kind = FailureKind.UNCERTAIN_WRITE if write else FailureKind.TRANSIENT_READ
        raise ProviderError(kind, provider, message)
    raise ProviderError(FailureKind.DEFINITE_REJECTION, provider, message)


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    write: bool = False,
    fault_after_success: Callable[[], bool] | None = None,
    **kwargs: object,
) -> httpx.Response:
    try:
        response = client.request(method, url, **kwargs)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        kind = FailureKind.UNCERTAIN_WRITE if write else FailureKind.TRANSIENT_READ
        raise ProviderError(kind, provider, type(exc).__name__) from exc
    classify_response(response, provider, write)
    if write and fault_after_success and fault_after_success():
        raise ProviderError(FailureKind.UNCERTAIN_WRITE, provider, "injected response loss after real write")
    return response

