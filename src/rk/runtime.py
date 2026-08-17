"""Injectable clock and UUIDv7 generation."""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


class Uuid7Generator:
    """Small dependency-free UUIDv7 generator with injectable entropy and time."""

    def __init__(
        self,
        *,
        time_ns: Callable[[], int] = time.time_ns,
        randbits: Callable[[int], int] = secrets.randbits,
    ) -> None:
        self._time_ns = time_ns
        self._randbits = randbits

    def new(self) -> str:
        timestamp_ms = self._time_ns() // 1_000_000
        if not 0 <= timestamp_ms < 1 << 48:
            raise OverflowError("UUIDv7 millisecond timestamp is out of range")
        rand_a = self._randbits(12)
        rand_b = self._randbits(62)
        value = timestamp_ms << 80
        value |= 0x7 << 76
        value |= rand_a << 64
        value |= 0b10 << 62
        value |= rand_b
        return str(uuid.UUID(int=value))
