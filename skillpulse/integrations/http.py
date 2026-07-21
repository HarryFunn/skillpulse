"""Small dependency-free JSON HTTP client used by integrations."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import IntegrationError

Query = Union[Mapping[str, Any], Sequence[tuple[str, Any]]]


class JsonHttpClient:
    def __init__(self, base_url: str, headers: Mapping[str, str] | None = None,
                 timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Accept": "application/json", **(headers or {})}
        self.timeout = timeout

    def get(self, path: str, params: Query | None = None) -> dict[str, Any]:
        query = urlencode(params or {}, doseq=True)
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        request = Request(url, headers=self.headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise IntegrationError(
                f"GET {path} returned HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise IntegrationError(f"GET {path} failed: {exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IntegrationError(f"GET {path} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise IntegrationError(f"GET {path} returned a non-object JSON response")
        return payload
