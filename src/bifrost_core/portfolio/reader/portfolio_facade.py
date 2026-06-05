"""Portfolio-facing DB operations split from StatusReader for clearer boundaries (R-M8 and extensions)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def get_model_analysis_for_account(conn: Any, account_id: str) -> Optional[Dict[str, Any]]:
    """Run portfolio model analysis (R-M8) for one IB account id. Caller manages transaction on conn."""
    from bifrost_core.portfolio.model import compute_model_analysis

    try:
        return compute_model_analysis(conn, account_id)
    except Exception as exc:
        logger.exception("get_model_analysis_for_account failed for %s: %s", account_id, exc)
        return None
