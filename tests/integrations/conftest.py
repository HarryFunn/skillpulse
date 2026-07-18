from __future__ import annotations

from typing import Any


class FakeJsonClient:
    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Any]] = []

    def get(self, path: str, params=None) -> dict[str, Any]:
        self.calls.append((path, params))
        queue = self.responses.get(path)
        if not queue:
            raise AssertionError(f"unexpected request: {path} {params}")
        return queue.pop(0)
