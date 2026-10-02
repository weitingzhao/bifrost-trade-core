"""Reader failures that must not look like empty results."""

from __future__ import annotations


class ReadFailed(RuntimeError):
    """The store could not be read -- distinct from a read that found nothing.

    Readers used to end in ``except Exception: return []``, so a database hiccup
    reached the UI as "no rules" or "0 open · 0 closed" over a full book, and
    nothing downstream could tell the two apart (debt TD-08). A reader that
    raises this lets the API answer 503 with the reason instead.
    """
