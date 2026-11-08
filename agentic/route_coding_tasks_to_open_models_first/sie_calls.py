"""One way to call SIE: record every call, stop on a refusal, carry on after a passing failure."""

from __future__ import annotations

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
    """SIE refused a call in a way that would fail every call. The run stops."""


class CallFailed(Exception):
    """One SIE call failed or replied without what was expected. A failed router call sends the
    task to Claude; a failed model call ends that attempt, and the task moves up a level."""


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
    """A refusal that fails every call the same way: a bad key, no credits, an unknown model.

    A rate limit, an over-long input, a timeout, a lost connection, a model still loading and a
    5xx are passing or per-item failures. So is a conversation that has outgrown the model's
    context window, which SIE reports as a plain request error.
    """
    if isinstance(error, ModelLoadFailedError):
        return bool(error.permanent)
    if "context_length" in str(error):
        return False
    return isinstance(error, RequestError) and not isinstance(
        error, (RateLimitError, InputTooLongError)
    )


def credits_of(reply: Any) -> float | None:
    """What one call was charged, from the metadata SIE attaches to its reply.

    An extract reply carries it under "request", and a batch repeats it on every item, so only the
    first counts. A chat reply carries it under "usage".
    """
    first = reply[0] if isinstance(reply, list) and reply else reply
    if not isinstance(first, dict):
        return None
    request, usage = first.get("request"), first.get("usage")
    if isinstance(request, dict):
        value = request.get("credits_debited_decimal", request.get("credits_debited"))
    elif isinstance(usage, dict):
        value = usage.get("credits_charged_decimal", usage.get("credits_charged"))
    else:
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def short(error: SIEError) -> str:
    return f"{type(error).__name__}: {str(error)[:ERROR_CHARS]}"


@dataclass
class Calls:
    """Every SIE call the run made, and the failures among them."""

    count: dict[str, int] = field(default_factory=dict)  # model -> calls
    failures: list[str] = field(default_factory=list)
    credits: dict[str, float] = field(default_factory=dict)  # model -> SIE credits charged
    unreported: int = 0  # successful calls whose reply didn't say what they were charged

    @property
    def total(self) -> int:
        return sum(self.count.values())

    def call(
        self, model: str, work: Callable[[], Any], valid: Callable[[Any], bool], expected: str
    ) -> Any:
        """Run one SIE call and return its reply, which `valid` must accept."""
        self.count[model] = self.count.get(model, 0) + 1
        try:
            reply = jsonable(work())
        except SIEError as error:
            if is_fatal(error):
                raise SIEStopped(
                    f"SIE refused the call to {model}, {short(error)}. Check SIE_API_KEY, "
                    "SIE_BASE_URL, the model name in rules.yaml and your SIE account."
                ) from error
            self.failures.append(f"{model}: {short(error)}")
            raise CallFailed(f"the SIE call failed: {short(error)}") from error
        if not valid(reply):
            self.failures.append(f"{model}: replied without {expected}")
            raise CallFailed(f"{model} replied without {expected}")
        charged = credits_of(reply)
        if charged is None:
            self.unreported += 1
        else:
            self.credits[model] = self.credits.get(model, 0.0) + charged
        return reply
