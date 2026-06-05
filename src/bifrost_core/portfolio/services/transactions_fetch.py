"""Fetch IB Flex cash transactions and upsert into account_transactions."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from bifrost_socket.ib.connector.flex_client import fetch_cash_transactions
from bifrost_core.monitor.reader import upsert_account_transactions

logger = logging.getLogger(__name__)


def fetch_cash_transactions_from_flex(
    reader: Any,
    control_via_db: Optional[dict],
    body: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Mirror POST /transactions/fetch behavior (no HTTP)."""
    try:
        entries: List[tuple] = []
        flex_list = reader.get_flex_config(purpose="cash_transactions")
        for a in flex_list:
            tok = (a.get("token") or "").strip()
            qid = (a.get("query_id") or "").strip()
            if tok and qid:
                entries.append((tok, qid))
        if not entries:
            return {
                "ok": False,
                "error": "No Flex credentials: configure in Settings → IB Connection → Flex (token and query_id with purpose cash_transactions).",
                "count": 0,
            }
        if not control_via_db:
            return {"ok": False, "error": "Postgres config required to write account_transactions.", "count": 0}
        payload = body or {}
        from_date = (payload.get("from_date") or "").strip() or None
        to_date = (payload.get("to_date") or "").strip() or None
        if from_date is None and to_date is None:
            from_date, to_date = reader.get_flex_default_range_dates()
        all_rows: List[Dict[str, Any]] = []
        errors: List[str] = []
        for token, query_id in entries:
            try:
                rows = fetch_cash_transactions(token, query_id, from_date=from_date, to_date=to_date)
                all_rows.extend(rows)
            except ValueError as e:
                errors.append(str(e))
        if errors and not all_rows:
            return {"ok": False, "error": "; ".join(errors), "count": 0}
        if not all_rows:
            return {"ok": True, "count": 0, "message": "No cash transactions in report.", "by_account": len(entries)}
        n = upsert_account_transactions(control_via_db, all_rows)
        msg = f"Upserted {n} transaction(s) from {len(entries)} Flex account(s)."
        if errors:
            msg += " Partial errors: " + "; ".join(errors)
        return {"ok": True, "count": n, "message": msg, "by_account": len(entries)}
    except Exception as e:
        logger.exception("fetch_cash_transactions_from_flex failed: %s", e)
        return {"ok": False, "error": str(e), "count": 0}
