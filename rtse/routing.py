"""Shared route types: the params dict every route takes, the route type, and ApiError (the HTTP error a route raises)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Params = dict[str, Any]
Route = Callable[[Params], dict[str, Any]]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
