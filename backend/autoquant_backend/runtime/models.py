from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceLog:
    sequence: int
    level: str
    symbol: str
    message: str
    created_at: int


