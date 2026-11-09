"""One way to call SIE: record every call, stop on a refusal, carry on after a passing failure."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sie_sdk.client.errors import (
    InputTooLongError,
    ModelLoadFailedError,
    RateLimitError,
    RequestError,
    SIEError,
)

WAIT_S = 900  # how long one call may wait for SIE Serverless to start a cold model
ERROR_CHARS = 200


class SIEStopped(Exception):
    """SIE refused a call in a way that would fail every call. The server returns it as a tool
    error starting "SIE refused", and recipe.py stops the run when it sees one."""


class CallFailed(Exception):
    """One SIE call failed or replied without what was expected. The server turns it into a tool
    error, so Claude never gets a resume that wasn't fully read and masked."""


def jsonable(value: Any) -> Any:
    """Turn SDK results (numpy arrays, nested dicts and lists) into plain JSON values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def is_fatal(error: SIEError) -> bool:
    """A refusal that fails every call the same way: a bad key, no credits, an unknown model, or any
    other 4xx refusal.

    A rate limit, an over-long input, a timeout, a lost connection, a model still loading and a
    5xx are passing or per-item failures.
    """
    if isinstance(error, ModelLoadFailedError):
        return bool(error.permanent)
    return isinstance(error, RequestError) and not isinstance(
        error, (RateLimitError, InputTooLongError)
    )


def short(error: SIEError) -> str:
    return f"{type(error).__name__}: {str(error)[:ERROR_CHARS]}"


@dataclass
class Calls:
    """Every SIE call the run made, and the failures among them. The server runs tools on worker
    threads, so the counts are updated under a lock."""

    count: dict[str, int] = field(default_factory=dict)  # model -> calls
    failures: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def total(self) -> int:
        return sum(self.count.values())

    def call(
        self, model: str, work: Callable[[], Any], valid: Callable[[Any], bool], expected: str
    ) -> Any:
        """Run one SIE call and return its reply, which `valid` must accept."""
        with self.lock:
            self.count[model] = self.count.get(model, 0) + 1
        try:
            reply = jsonable(work())
        except SIEError as error:
            if is_fatal(error):
                raise SIEStopped(
                    f"SIE refused the call to {model}, {short(error)}. Check SIE_API_KEY, "
                    "SIE_BASE_URL, the model name in rules.yaml and your SIE account."
                ) from error
            with self.lock:
                self.failures.append(f"{model}: {short(error)}")
            raise CallFailed(f"the SIE call failed: {short(error)}") from error
        if not valid(reply):
            with self.lock:
                self.failures.append(f"{model}: replied without {expected}")
            raise CallFailed(f"{model} replied without {expected}")
        return reply
