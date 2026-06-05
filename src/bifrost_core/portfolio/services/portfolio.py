"""Orchestration helpers for monitor portfolio / model analysis (delegates to StatusReader)."""

from __future__ import annotations

from typing import Any, Dict, Optional


def run_model_analysis_for_account(reader: Any, account_id: str) -> Optional[Dict[str, Any]]:
    """R-M8 model analysis for one account — preferred entry for new code (wraps reader.get_model_analysis)."""
    return reader.get_model_analysis(account_id)
