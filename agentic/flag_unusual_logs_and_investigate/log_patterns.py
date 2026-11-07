"""Turn a log line into its pattern: the message with the values that change replaced.

`payments ERROR decrypt failed for order 10234` and `... order 10517` are one pattern. Both
make_data.py and recipe.py use this, so the known patterns and the day's lines group the same way.
"""

from __future__ import annotations

import re

# Most specific first, so an email address isn't split into numbers.
VALUES = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<id>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<id>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
)


def pattern_of(service: str, level: str, message: str) -> str:
    """The line's pattern, with its service and level in front."""
    for value, placeholder in VALUES:
        message = value.sub(placeholder, message)
    return f"{service} {level} {message}"
