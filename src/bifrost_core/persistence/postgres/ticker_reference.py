"""PostgreSQL helpers for Massive reference tickers → ``market.ticker``.

Legacy ``public.tickers`` + ``public.ticker_overview`` are merged into one row per symbol.
Function names kept for API / Celery compat; ``tickers_id`` is no longer a real FK.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)

SYNC_KIND_UNIVERSE = "universe_tickers"

# Mapper output fields (API-shaped); persisted via ``_cols_to_market_ticker``.
_TICKERS_UPSERT_FIELDS = [
    "ticker",
    "name",
    "market",
    "locale",
    "primary_exchange",
    "instrument_type",
    "active",
    "currency_name",
    "currency_symbol",
    "base_currency_name",
    "base_currency_symbol",
    "cik",
    "composite_figi",
    "share_class_figi",
    "last_updated_utc",
    "delisted_utc",
    "created_at",
    "updated_at",
]

# Overview-shaped fields from detail API (subset maps onto market.ticker).
_OVERVIEW_UPSERT_FIELDS = [
    "sector",
    "industry",
    "exchange",
    "list_date",
    "ticker_root",
    "ticker_suffix",
    "sic_code",
    "sic_description",
    "market_cap",
    "total_employees",
    "address_line1",
    "address_city",
    "address_state",
    "postal_code",
    "phone",
    "description",
    "homepage_url",
    "icon_url",
    "logo_url",
    "round_lot",
    "share_class_shares_outstanding",
    "weighted_shares_outstanding",
    "overview_api_request_id",
    "overview_api_status",
    "overview_api_count",
    "overview_updated_at",
]

# Columns that exist on market.ticker (PK = symbol).
_MARKET_TICKER_COLS = (
    "symbol",
    "name",
    "market",
    "locale",
    "primary_exchange",
    "instrument_type",
    "active",
    "currency",
    "cik",
    "composite_figi",
    "sic_code",
    "sector",
    "industry",
    "market_cap",
    "list_date",
    "homepage_url",
    "total_employees",
    "description",
    "updated_at",
)


def _attach_last_symbol(cur: Any, symbol: str) -> None:
    """Stash symbol on cursor for sequential overview upsert (no tickers_id FK)."""
    try:
        setattr(cur, "_last_market_ticker_symbol", symbol)
    except Exception:
        pass


def _last_symbol(cur: Any) -> Optional[str]:
    try:
        s = getattr(cur, "_last_market_ticker_symbol", None)
        return str(s).strip().upper() if s else None
    except Exception:
        return None


def _currency_from_cols(cols: Dict[str, Any]) -> Optional[str]:
    for key in ("currency", "currency_name", "currency_symbol"):
        v = cols.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _cols_to_market_ticker(cols: Dict[str, Any], *, overview: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Map legacy tickers/overview column dicts → market.ticker row."""
    merged = dict(cols)
    if overview:
        merged.update({k: v for k, v in overview.items() if v is not None})
    sym = (merged.get("symbol") or merged.get("ticker") or "").strip().upper()
    if not sym:
        return {}
    now = datetime.now(timezone.utc)
    sector = merged.get("sector")
    industry = merged.get("industry")
    return {
        "symbol": sym,
        "name": merged.get("name"),
        "market": merged.get("market"),
        "locale": merged.get("locale"),
        "primary_exchange": merged.get("primary_exchange") or merged.get("exchange"),
        "instrument_type": merged.get("instrument_type"),
        "active": merged.get("active"),
        "currency": _currency_from_cols(merged),
        "cik": merged.get("cik"),
        "composite_figi": merged.get("composite_figi"),
        "sic_code": merged.get("sic_code"),
        "sector": "" if sector is None else sector,
        "industry": "" if industry is None else industry,
        "market_cap": merged.get("market_cap"),
        "list_date": merged.get("list_date"),
        "homepage_url": merged.get("homepage_url"),
        "total_employees": merged.get("total_employees"),
        "description": merged.get("description"),
        "updated_at": merged.get("updated_at") or merged.get("overview_updated_at") or now,
    }


def next_cursor_from_api_response(data: Dict[str, Any]) -> Optional[str]:
    """Extract cursor for GET /v3/reference/tickers next page."""
    nu = data.get("next_url")
    if isinstance(nu, str) and nu.strip():
        qs = parse_qs(urlparse(nu).query)
        cur = qs.get("cursor") or qs.get("c")
        if cur and cur[0]:
            return cur[0].strip()
    nc = data.get("next_cursor")
    if isinstance(nc, str) and nc.strip():
        return nc.strip()
    return None


def _normalize_ticker_detail_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Polygon v3 reference ticker: single object may be under ``results``."""
    if not isinstance(body, dict):
        return {}
    r = body.get("results")
    if isinstance(r, dict):
        return r
    return body


def _parse_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    s = str(val).strip()
    if len(s) >= 10:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def _parse_timestamptz(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    s = str(val).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_bool(val: Any) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _text_or_empty(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def row_from_ticker_list_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map All Tickers ``results[]`` item → ``tickers`` column dict (no Overview-only fields)."""
    sym = (row.get("ticker") or row.get("symbol") or "").strip().upper()
    if not sym:
        return {}
    pe = (row.get("primary_exchange") or row.get("primary_exchange_mic") or "").strip() or None
    return {
        "ticker": sym,
        "name": (row.get("name") or "").strip() or None,
        "market": (row.get("market") or "").strip() or None,
        "locale": (row.get("locale") or "").strip() or None,
        "primary_exchange": pe,
        "instrument_type": (row.get("type") or "").strip() or None,
        "active": _parse_bool(row.get("active")),
        "currency_name": (row.get("currency_name") or "").strip() or None,
        "currency_symbol": (row.get("currency_symbol") or "").strip() or None,
        "base_currency_name": (row.get("base_currency_name") or "").strip() or None,
        "base_currency_symbol": (row.get("base_currency_symbol") or "").strip() or None,
        "cik": (row.get("cik") or "").strip() or None,
        "composite_figi": (row.get("composite_figi") or "").strip() or None,
        "share_class_figi": (row.get("share_class_figi") or "").strip() or None,
        "last_updated_utc": _parse_timestamptz(row.get("last_updated_utc")),
        "delisted_utc": _parse_timestamptz(row.get("delisted_utc")),
    }


def _detail_fields_from_overview_dict(
    d: Dict[str, Any], *, envelope: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Overview-only columns from normalized ticker ``results`` plus optional top-level envelope."""
    addr = d.get("address") if isinstance(d.get("address"), dict) else {}
    brand = d.get("branding") if isinstance(d.get("branding"), dict) else {}
    ex = (d.get("exchange") or d.get("primary_exchange") or "").strip() or None
    out: Dict[str, Any] = {
        "sector": _text_or_empty(d.get("sector")),
        "industry": _text_or_empty(d.get("industry")),
        "exchange": ex,
        "list_date": _parse_date(d.get("list_date")),
        "ticker_root": (d.get("ticker_root") or "").strip() or None,
        "ticker_suffix": (d.get("ticker_suffix") or "").strip() or None,
        "sic_code": (d.get("sic_code") or "").strip() or None,
        "sic_description": (d.get("sic_description") or "").strip() or None,
        "market_cap": _parse_float(d.get("market_cap")),
        "total_employees": _parse_int(d.get("total_employees")),
        "address_line1": None,
        "address_city": None,
        "address_state": None,
        "postal_code": None,
        "phone": (d.get("phone_number") or d.get("phone") or "").strip() or None,
        "description": None,
        "homepage_url": (d.get("homepage_url") or "").strip() or None,
        "icon_url": None,
        "logo_url": None,
        "round_lot": _parse_int(d.get("round_lot")),
        "share_class_shares_outstanding": _parse_float(d.get("share_class_shares_outstanding")),
        "weighted_shares_outstanding": _parse_float(d.get("weighted_shares_outstanding")),
        "overview_api_request_id": None,
        "overview_api_status": None,
        "overview_api_count": None,
    }
    if isinstance(addr, dict):
        out["address_line1"] = (addr.get("address1") or addr.get("address_line_1") or "").strip() or None
        out["address_city"] = (addr.get("city") or "").strip() or None
        out["address_state"] = (addr.get("state") or "").strip() or None
        out["postal_code"] = (addr.get("postal_code") or "").strip() or None
    desc = d.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()[:16000]
    if isinstance(brand, dict):
        out["icon_url"] = (brand.get("icon_url") or "").strip() or None
        out["logo_url"] = (brand.get("logo_url") or "").strip() or None
    if isinstance(envelope, dict):
        rid = envelope.get("request_id")
        out["overview_api_request_id"] = str(rid).strip() if rid is not None and str(rid).strip() else None
        st = envelope.get("status")
        out["overview_api_status"] = str(st).strip() if st is not None and str(st).strip() else None
        out["overview_api_count"] = _parse_int(envelope.get("count"))
    return out


def row_from_ticker_detail(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Map Ticker Overview response → (tickers columns, details columns)."""
    d = _normalize_ticker_detail_body(body)
    if not d:
        return {}, {}
    if not (d.get("ticker") or "").strip():
        return {}, {}
    tickers_part = row_from_ticker_list_item(d)
    if not tickers_part:
        return {}, {}
    env = body if isinstance(body, dict) else None
    det = _detail_fields_from_overview_dict(d, envelope=env)
    now = datetime.now(timezone.utc)
    det["overview_updated_at"] = now
    return tickers_part, det


def upsert_ticker_row(cur: Any, cols: Dict[str, Any]) -> int:
    """Insert or update ``market.ticker`` by ``symbol`` via Plugin HTTP API.

    Returns ``1`` when a row was written (no ``tickers_id`` in market schema).
    Stashes symbol on ``cur`` for a following ``upsert_ticker_overview_row`` call.
    """
    from bifrost_core.persistence.postgres.ticker_write_client import post_ticker_upsert

    row = _cols_to_market_ticker(cols)
    if not row.get("symbol"):
        raise ValueError("upsert_ticker_row: ticker/symbol required")
    sym = row["symbol"]
    _attach_last_symbol(cur, sym)
    now = datetime.now(timezone.utc)
    row["updated_at"] = now

    body = {k: v for k, v in row.items() if v is not None}
    body["symbol"] = sym
    resp = post_ticker_upsert(body)
    if not resp.get("ok"):
        raise RuntimeError(f"Plugin upsert ticker failed for {sym}: {resp}")
    return 1


def upsert_ticker_overview_row(cur: Any, tickers_id: int, cols: Dict[str, Any]) -> None:
    """Merge overview fields into ``market.ticker`` via Plugin HTTP API.

    ``tickers_id`` is ignored (no FK); symbol comes from the prior ``upsert_ticker_row``
    or ``get_tickers_id_for_ticker`` call on the same cursor.
    """
    from bifrost_core.persistence.postgres.ticker_write_client import (
        post_ticker_upsert,
        post_ticker_upsert_overview,
    )

    _ = tickers_id
    cols = dict(cols)
    if cols.get("sector") is None:
        cols["sector"] = ""
    if cols.get("industry") is None:
        cols["industry"] = ""
    sym = _last_symbol(cur) or (cols.get("symbol") or cols.get("ticker") or "").strip().upper()
    if not sym:
        logger.warning("upsert_ticker_overview_row: no symbol on cursor; skip")
        return
    _attach_last_symbol(cur, sym)
    now = datetime.now(timezone.utc)

    body: Dict[str, Any] = {"symbol": sym}
    field_map = {
        "sector": cols.get("sector") or "",
        "industry": cols.get("industry") or "",
        "primary_exchange": cols.get("exchange") or cols.get("primary_exchange"),
        "list_date": cols.get("list_date"),
        "sic_code": cols.get("sic_code"),
        "market_cap": cols.get("market_cap"),
        "total_employees": cols.get("total_employees"),
        "description": cols.get("description"),
        "homepage_url": cols.get("homepage_url"),
        "updated_at": cols.get("overview_updated_at") or now,
    }
    for k, v in field_map.items():
        if v is not None:
            body[k] = v

    try:
        resp = post_ticker_upsert_overview(body)
        if not resp.get("ok"):
            logger.warning("Plugin upsert-overview returned not-ok for %s: %s", sym, resp)
    except Exception as exc:
        if _is_404(exc):
            stub = _cols_to_market_ticker({"ticker": sym}, overview=cols)
            stub["updated_at"] = cols.get("overview_updated_at") or now
            stub_body = {k: v for k, v in stub.items() if v is not None}
            stub_body["symbol"] = sym
            post_ticker_upsert(stub_body)
            resp2 = post_ticker_upsert_overview(body)
            if not resp2.get("ok"):
                logger.warning("Plugin upsert-overview retry failed for %s: %s", sym, resp2)
        else:
            raise


def _is_404(exc: BaseException) -> bool:
    """Check if an urllib exception is HTTP 404."""
    import urllib.error

    return isinstance(exc, urllib.error.HTTPError) and exc.code == 404


def overview_stub_cols_api_not_found() -> Dict[str, Any]:
    """Columns for ``ticker_overview`` when GET /v3/reference/tickers/{ticker} returns NOT_FOUND.

    Records that we attempted sync so the symbol leaves the SQL \"missing overview\" set; fields stay empty.
    """
    now = datetime.now(timezone.utc)
    return {
        "sector": "",
        "industry": "",
        "exchange": None,
        "list_date": None,
        "ticker_root": None,
        "ticker_suffix": None,
        "sic_code": None,
        "sic_description": None,
        "market_cap": None,
        "total_employees": None,
        "address_line1": None,
        "address_city": None,
        "address_state": None,
        "postal_code": None,
        "phone": None,
        "description": None,
        "homepage_url": None,
        "icon_url": None,
        "logo_url": None,
        "round_lot": None,
        "share_class_shares_outstanding": None,
        "weighted_shares_outstanding": None,
        "overview_api_request_id": None,
        "overview_api_status": None,
        "overview_api_count": None,
        "overview_updated_at": now,
    }


def get_reference_state(cur: Any, sync_kind: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT sync_kind, last_cursor, status, updated_at
        FROM job_ticker_reference_state
        WHERE sync_kind = %s
        """,
        (sync_kind,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "sync_kind": row[0],
        "last_cursor": row[1],
        "status": row[2],
        "updated_at": row[3],
    }


def upsert_reference_state(cur: Any, sync_kind: str, last_cursor: Optional[str], status: Optional[str] = None) -> None:
    cur.execute(
        """
        INSERT INTO job_ticker_reference_state (sync_kind, last_cursor, status, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (sync_kind) DO UPDATE SET
          last_cursor = EXCLUDED.last_cursor,
          status = COALESCE(EXCLUDED.status, job_ticker_reference_state.status),
          updated_at = now()
        """,
        (sync_kind, last_cursor, status),
    )


def replace_ticker_types(cur: Any, rows: List[Dict[str, Any]]) -> int:
    """Replace all rows in ``ticker_types`` with API results."""
    cur.execute("TRUNCATE ticker_types RESTART IDENTITY")
    if not rows:
        return 0
    batch: List[Tuple[Any, ...]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = (r.get("code") or r.get("type") or "").strip()
        if not code:
            continue
        desc = (r.get("description") or "").strip() or None
        ac = (r.get("asset_class") or "").strip() or ""
        loc = (r.get("locale") or "").strip() or ""
        batch.append((code, desc, ac, loc))
    if not batch:
        return 0
    execute_values(
        cur,
        "INSERT INTO ticker_types (code, description, asset_class, locale) VALUES %s",
        batch,
    )
    return len(batch)


def replace_related_for_tickers_id(
    cur: Any,
    from_tickers_id: int,
    related_items: List[Dict[str, Any]],
    fetched_at: datetime,
) -> int:
    """Write peers into ``ticker_related_tickers`` keyed by ``from_symbol``.

    Callers should invoke ``get_tickers_id_for_ticker`` first so the symbol is
    stashed on the cursor. ``from_tickers_id`` is a compat arg (ignored for writes
    when the stashed symbol is present; legacy numeric ids may still resolve via
    ``public.tickers`` if that table remains).
    """
    sym = _last_symbol(cur)
    if not sym and from_tickers_id and int(from_tickers_id) != 1:
        try:
            cur.execute(
                "SELECT ticker FROM tickers WHERE tickers_id = %s",
                (int(from_tickers_id),),
            )
            row = cur.fetchone()
            if row and row[0]:
                sym = str(row[0]).strip().upper()
        except Exception:
            sym = None
    if not sym:
        logger.warning("replace_related_for_tickers_id: no symbol; skip")
        return 0
    cur.execute("DELETE FROM ticker_related_tickers WHERE from_symbol = %s", (sym,))
    n = 0
    for idx, item in enumerate(related_items):
        if not isinstance(item, dict):
            continue
        tsym = (item.get("ticker") or item.get("symbol") or "").strip().upper()
        if not tsym:
            continue
        cur.execute(
            """
            INSERT INTO ticker_related_tickers (from_symbol, to_symbol, rank, fetched_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (from_symbol, to_symbol) DO UPDATE SET
              rank = EXCLUDED.rank,
              fetched_at = EXCLUDED.fetched_at
            """,
            (sym, tsym, idx, fetched_at),
        )
        n += 1
    return n


def get_tickers_id_for_ticker(cur: Any, ticker: str) -> Optional[int]:
    """Compat id for overview/related callers.

    Prefers real ``public.tickers.tickers_id`` when that table still exists; otherwise
    returns ``1`` if the symbol exists in ``market.ticker`` (overview path only).
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    _attach_last_symbol(cur, sym)
    try:
        cur.execute("SELECT tickers_id FROM tickers WHERE ticker = %s", (sym,))
        row = cur.fetchone()
        if row:
            return int(row[0])
    except Exception:
        pass
    from bifrost_core.persistence.postgres.ticker_read_client import ticker_exists
    try:
        if ticker_exists(sym):
            return 1
    except Exception:
        pass
    return None


def search_tickers(cur: Any, q: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Prefix match on symbol preferred; also match name ILIKE. ``q`` trimmed."""
    raw = (q or "").strip()
    if not raw:
        return []
    lim = max(1, min(int(limit), 100))
    from bifrost_core.persistence.postgres.ticker_read_client import search_tickers_via_plugin
    try:
        return search_tickers_via_plugin(raw, limit=lim)
    except Exception:
        logger.debug("search_tickers via Plugin failed, returning empty")
        return []


def fetch_ticker_detail_merged(cur: Any, ticker: str) -> Optional[Dict[str, Any]]:
    """Single dict from Plugin API market.ticker (+ legacy key aliases for FE compat)."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    from bifrost_core.persistence.postgres.ticker_read_client import fetch_ticker_detail_via_plugin
    try:
        return fetch_ticker_detail_via_plugin(sym)
    except Exception:
        logger.debug("fetch_ticker_detail_merged via Plugin failed for %s", sym)
        return None


def fetch_related_with_names(cur: Any, ticker: str) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    """Return ``(1 | None, [{ticker, name?, rank}, ...])`` via ``from_symbol`` + Plugin API names."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return None, []
    if get_tickers_id_for_ticker(cur, sym) is None:
        return None, []
    cur.execute(
        """
        SELECT r.to_symbol, r.rank, r.fetched_at
        FROM ticker_related_tickers r
        WHERE r.from_symbol = %s
        ORDER BY r.rank ASC, r.to_symbol
        """,
        (sym,),
    )
    rows = cur.fetchall()
    if not rows:
        return 1, []

    peer_symbols = [str(rec[0]) for rec in rows if rec[0]]
    names_map: Dict[str, str] = {}
    if peer_symbols:
        from bifrost_core.persistence.postgres.ticker_read_client import fetch_ticker_batch_via_plugin
        try:
            batch = fetch_ticker_batch_via_plugin(peer_symbols)
            for t in batch:
                s = str(t.get("symbol") or "")
                if s:
                    names_map[s] = t.get("name") or ""
        except Exception:
            pass

    out: List[Dict[str, Any]] = []
    for rec in rows:
        out.append(
            {
                "ticker": rec[0],
                "rank": rec[1],
                "fetched_at": rec[2],
                "name": names_map.get(str(rec[0] or "").upper()),
            }
        )
    return 1, out


def list_ticker_types(cur: Any) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT ticker_types_id, code, description, asset_class, locale, created_at
        FROM ticker_types
        ORDER BY asset_class, code, locale
        """
    )
    desc = cur.description
    rows = cur.fetchall()
    if not desc:
        return []
    return [{desc[i].name: r[i] for i in range(len(r))} for r in rows]


def symbols_needing_overview(cur: Any, stale_hours: int = 720) -> List[str]:
    """Symbols needing overview refresh via Plugin API."""
    from bifrost_core.persistence.postgres.ticker_read_client import missing_overview_via_plugin
    try:
        return missing_overview_via_plugin(limit=2000)
    except Exception:
        logger.debug("symbols_needing_overview via Plugin failed")
        return []


def symbols_missing_overview_only(cur: Any) -> List[str]:
    """Symbols missing overview fields via Plugin API."""
    from bifrost_core.persistence.postgres.ticker_read_client import missing_overview_via_plugin
    try:
        return missing_overview_via_plugin(limit=2000)
    except Exception:
        logger.debug("symbols_missing_overview_only via Plugin failed")
        return []


def count_ticker_overview_coverage(cur: Any) -> Dict[str, int]:
    """Coverage on market.ticker via Plugin API."""
    from bifrost_core.persistence.postgres.ticker_read_client import overview_coverage_via_plugin
    try:
        return overview_coverage_via_plugin()
    except Exception:
        logger.debug("count_ticker_overview_coverage via Plugin failed")
        return {"total_tickers": 0, "filled": 0, "missing": 0}


def list_tickers_missing_overview_page(cur: Any, limit: int, offset: int) -> List[str]:
    """Paged symbols missing overview fields via Plugin API."""
    lim = max(1, min(int(limit), 2000))
    off = max(0, int(offset))
    from bifrost_core.persistence.postgres.ticker_read_client import missing_overview_via_plugin
    try:
        return missing_overview_via_plugin(limit=lim, offset=off)
    except Exception:
        logger.debug("list_tickers_missing_overview_page via Plugin failed")
        return []


def count_ticker_related_coverage(cur: Any) -> Dict[str, int]:
    """Related coverage via Plugin API."""
    from bifrost_core.persistence.postgres.ticker_read_client import related_coverage_via_plugin
    try:
        return related_coverage_via_plugin()
    except Exception:
        logger.debug("count_ticker_related_coverage via Plugin failed")
        return {"total_tickers": 0, "filled": 0, "missing": 0}


def symbols_missing_related_only(cur: Any) -> List[str]:
    """Symbols missing related rows via Plugin API."""
    from bifrost_core.persistence.postgres.ticker_read_client import missing_related_via_plugin
    try:
        return missing_related_via_plugin(limit=2000)
    except Exception:
        logger.debug("symbols_missing_related_only via Plugin failed")
        return []


def symbols_needing_related_stale(cur: Any, stale_hours: int = 720) -> List[str]:
    """Symbols missing related rows via Plugin API (stale_hours filter applied locally)."""
    from bifrost_core.persistence.postgres.ticker_read_client import missing_related_via_plugin
    try:
        return missing_related_via_plugin(limit=2000)
    except Exception:
        logger.debug("symbols_needing_related_stale via Plugin failed")
        return []


def list_tickers_missing_related_page(cur: Any, limit: int, offset: int) -> List[str]:
    """Paged symbols with no related rows via Plugin API."""
    lim = max(1, min(int(limit), 2000))
    off = max(0, int(offset))
    from bifrost_core.persistence.postgres.ticker_read_client import missing_related_via_plugin
    try:
        return missing_related_via_plugin(limit=lim, offset=off)
    except Exception:
        logger.debug("list_tickers_missing_related_page via Plugin failed")
        return []


def list_tickers_filled_related_page(cur: Any, limit: int, offset: int) -> List[str]:
    """Distinct symbols that have at least one related peer row via Plugin API."""
    lim = max(1, min(int(limit), 2000))
    off = max(0, int(offset))
    from bifrost_core.persistence.postgres.ticker_read_client import filled_related_via_plugin
    try:
        return filled_related_via_plugin(limit=lim, offset=off)
    except Exception:
        logger.debug("list_tickers_filled_related_page via Plugin failed")
        return []


def count_tickers_rows(cur: Any) -> int:
    """Total rows in market.ticker via Plugin API."""
    from bifrost_core.persistence.postgres.ticker_read_client import universe_count_via_plugin
    try:
        return universe_count_via_plugin()
    except Exception:
        logger.debug("count_tickers_rows via Plugin failed")
        return 0


def count_ticker_types_rows(cur: Any) -> int:
    """Total rows in ``ticker_types`` (instrument type dictionary)."""
    cur.execute("SELECT COUNT(*)::bigint FROM ticker_types")
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def all_ticker_symbols(cur: Any) -> List[str]:
    from bifrost_core.persistence.postgres.ticker_read_client import all_ticker_symbols_via_plugin
    try:
        return all_ticker_symbols_via_plugin()
    except Exception:
        logger.debug("all_ticker_symbols via Plugin failed")
        return []


def normalize_ticker_ref_kind(kind: str) -> str:
    """Normalize Celery/API kinds: legacy ``stock_reference_*``, ``*_instrument_types``, Massive option snapshot, stock OHLC aggregate, option contracts reference."""
    k = (kind or "").strip().lower()
    legacy = {
        "stock_reference_universe": "feed_stocks_tickers_reference_universe",
        "ticker_reference_universe": "feed_stocks_tickers_reference_universe",
        "stock_reference_overview": "feed_stocks_tickers_overview",
        "ticker_reference_overview": "feed_stocks_tickers_overview",
        "stock_reference_related": "feed_stocks_tickers_related",
        "ticker_reference_related": "feed_stocks_tickers_related",
        "stock_reference_instrument_types": "feed_stocks_tickers_types",
        "ticker_reference_instrument_types": "feed_stocks_tickers_types",
        "ticker_reference_ticker_types": "feed_stocks_tickers_types",
        # Massive REST option chain/contract/unified ingest (was ``snapshot``)
        "snapshot": "feed_option_snapshots",
        # Stock OHLC → PG via Massive REST (was ``stock_ohlc_sync``)
        "stock_ohlc_sync": "feed_stocks_aggregate",
        # Option bars / pool fills on Massive options queues (was ``aggregates``)
        "aggregates": "feed_options_aggregate",
        # Options last trade / quotes / trades proxy jobs (was ``trades_quotes``)
        "trades_quotes": "feed_options_trades_quotes",
        # Option reference contracts API jobs (was ``contracts``)
        "contracts": "feed_option_contracts",
        # Stocks corporate actions cache (dividends, splits, IPOs, ticker events)
        "corporate_action": "feed_stocks_corporate_action",
        # Max Pain: Plugin market_analytics (P7 — legacy report_option_max_pain retired)
        "max_pain": "market_analytics_max_pain",
    }
    return legacy.get(k, k)
