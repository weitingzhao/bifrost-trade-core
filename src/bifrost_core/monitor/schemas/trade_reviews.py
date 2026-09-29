"""Request body for /strategies/reviews.

A review is the trader's own verdict on one closed instance: the tags the
rules missed (`tags_added`), the derived tags that do not apply
(`tags_dropped`), and whether the review is done. It is a record, never an
instruction -- nothing downstream reads it to act (D10).
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class TradeReviewBody(BaseModel):
    """Replace the review's tags; `reviewed` true stamps it done, false reopens it.

    A field left out keeps what is stored.
    """

    tags_added: Optional[List[str]] = Field(None, description="Tags the rules missed, as the trader wrote them")
    tags_dropped: Optional[List[str]] = Field(None, description="Keys of derived tags that do not apply")
    note: Optional[str] = Field(None, max_length=4000)
    reviewed: Optional[bool] = Field(None, description="true stamps reviewed_at; false clears it")
