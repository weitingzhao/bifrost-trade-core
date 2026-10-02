"""Reader and writer outcomes that must not look like success or an empty result.

Reads: ``ReadFailed`` (TD-08). Writes (TD-15): a writer either returns what it
wrote or raises one of the four ``Write*`` outcomes below, so the API can answer
404 / 409 / 422 / 503 with the writer's own reason instead of guessing from a
``False``. Every outcome carries ``reason``, a sentence the UI can show as it is.

    WriteError                    base; ``reason``
    ├── WriteNotFound             the target row does not exist           -> 404
    ├── WriteConflict             in use, or the row's state says no      -> 409
    │   ├── RuleInUseError        (strategy_rules_delete)
    │   └── PlanRuleError         (strategy_plan)
    ├── WriteInvalid              the input is wrong                      -> 422
    └── WriteFailed               database not configured / unreachable /
                                  the statement failed                    -> 503 / 500

``WriteNotFound`` is also a ``LookupError``, ``WriteInvalid`` a ``ValueError``
and ``WriteFailed`` a ``RuntimeError``, so a broad ``except`` that predates them
still sorts them the way it did. ``WriteConflict`` is deliberately *not* a
``ValueError``; its two older subclasses keep ``ValueError`` as a second base
because callers catch them that way today.
"""

from __future__ import annotations

from typing import Optional


class ReadFailed(RuntimeError):
    """The store could not be read -- distinct from a read that found nothing.

    Readers used to end in ``except Exception: return []``, so a database hiccup
    reached the UI as "no rules" or "0 open · 0 closed" over a full book, and
    nothing downstream could tell the two apart (debt TD-08). A reader that
    raises this lets the API answer 503 with the reason instead.
    """


class WriteError(Exception):
    """A write did not happen; ``reason`` says why, in words the UI can show."""

    default_reason = "The write did not happen."

    def __init__(self, reason: Optional[str] = None) -> None:
        text = reason if reason else self.default_reason
        super().__init__(text)
        self.reason = text


class WriteNotFound(WriteError, LookupError):
    """The row the write names does not exist (nothing was written)."""

    default_reason = "Not found."


class WriteConflict(WriteError):
    """The row exists but may not change this way: it is in use, or its state refuses."""

    default_reason = "The change conflicts with the stored state."


class WriteInvalid(WriteError, ValueError):
    """The input is wrong: empty, an unknown field, a bad value, or NULL for a required column."""

    default_reason = "Invalid input."


class WriteFailed(WriteError, RuntimeError):
    """The database could not be written: not configured, unreachable, or the statement failed."""

    default_reason = "The database write failed."


__all__ = [
    "ReadFailed",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteInvalid",
    "WriteNotFound",
]
