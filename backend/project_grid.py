"""Safe project-specific operations for the 588000 paper grid."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from backend import cn_market


ACCOUNT_ID = "acct_588000_grid"
SYMBOL = "588000.SH"
CODE = "588000"
GRID_FIELDS = (
    "lower_price",
    "upper_price",
    "reference_price",
    "spacing_pct",
    "order_quantity",
    "min_position",
    "max_position",
)


class ConfirmedFillRecorder:
    """Idempotently append a user-confirmed fill to the fixed paper account."""

    def __init__(self, db_path: str | Path, handler: Any):
        self.db_path = str(db_path)
        self.handler = handler
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmed_fill_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def record(self, fill: dict[str, Any]) -> dict[str, Any]:
        if fill.get("user_confirmed") is not True:
            raise ValueError("user_confirmed must be true; ask the user to confirm the extracted fill first")
        key = str(fill.get("idempotency_key") or "").strip()
        source = str(fill.get("source") or "").strip()
        if not key or len(key) > 200:
            raise ValueError("idempotency_key must be 1..200 characters")
        if not source or len(source) > 500:
            raise ValueError("source must describe the confirmation evidence")

        payload = {
            "account_id": ACCOUNT_ID,
            "symbol": SYMBOL,
            "side": str(fill.get("side") or "").strip().upper(),
            "quantity": fill.get("quantity"),
            "price": fill.get("price"),
            "trade_date": str(fill.get("trade_date") or "").strip(),
            "trade_time": str(fill.get("trade_time") or "15:00:00").strip(),
            "apply_fees": bool(fill.get("apply_fees", True)),
            "note": f"User-confirmed paper fill; source={source}; {str(fill.get('note') or '').strip()}".strip(),
        }
        digest = self._digest(payload)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM confirmed_fill_requests WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row:
                if row["payload_digest"] != digest:
                    raise ValueError("idempotency_key was already used with different fill data")
                if row["status"] == "completed":
                    return {"fill": json.loads(row["result_json"]), "created": False}
                raise ValueError(
                    f"this fill request is {row['status']}; inspect the ledger before trying a new key"
                )
            conn.execute(
                """INSERT INTO confirmed_fill_requests
                   (idempotency_key, payload_digest, status, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (key, digest, now, now),
            )

        try:
            self._reject_obvious_duplicate(payload)
            result = self.handler.trading.backfill_trade(payload)
        except Exception as exc:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE confirmed_fill_requests SET status='failed', error_text=?, updated_at=? WHERE idempotency_key=?",
                    (str(exc)[:2000], datetime.now(timezone.utc).isoformat(), key),
                )
            raise

        with self._connection() as conn:
            conn.execute(
                "UPDATE confirmed_fill_requests SET status='completed', result_json=?, updated_at=? WHERE idempotency_key=?",
                (json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), key),
            )
        return {"fill": result, "created": True}

    def _reject_obvious_duplicate(self, payload: dict[str, Any]) -> None:
        events = self.handler.store.list_events(
            {"event_type": "trade_filled", "account_id": ACCOUNT_ID, "symbol": SYMBOL, "limit": 100000}
        )
        target_prefix = f"{payload['trade_date']}T{payload['trade_time']}"
        for event in events:
            metadata = event.get("metadata") or {}
            if (
                str(event.get("timestamp") or "").startswith(target_prefix)
                and str(metadata.get("side") or event.get("side") or "").upper() == payload["side"]
                and int(event.get("quantity") or 0) == int(payload["quantity"])
                and abs(float(event.get("price") or 0) - float(payload["price"])) < 1e-9
            ):
                raise ValueError("an identical fill already exists in the ledger; no duplicate was written")


_FILLED_STATUSES = {"filled", "成交", "已成", "已成交"}
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _fill_key(fill: dict[str, Any]) -> tuple[str, str, int, float]:
    return (
        f"{fill['trade_date']}T{fill['trade_time']}",
        str(fill["side"]).upper(),
        int(fill["quantity"]),
        round(float(fill["price"]), 8),
    )


def confirmed_grid_fills_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract only explicit user-confirmed 588000 fills from research memory."""
    fills: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, float]] = set()
    for record in records:
        if record.get("verification") != "user_confirmed":
            continue
        content = record.get("content") or {}
        candidates: list[dict[str, Any]] = []
        for field in ("fill", "fills", "confirmed_fills"):
            value = content.get(field)
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
        for item in candidates:
            if str(item.get("symbol") or "").upper() != SYMBOL:
                continue
            if str(item.get("status") or "").strip().lower() not in _FILLED_STATUSES:
                continue
            side = str(item.get("side") or "").strip().upper()
            if side not in {"BUY", "SELL"}:
                continue
            try:
                quantity = int(item.get("quantity"))
                price = float(item.get("price"))
            except (TypeError, ValueError):
                continue
            if quantity <= 0 or quantity % 100 != 0 or not math.isfinite(price) or price <= 0:
                continue
            raw_time = item.get("time") or item.get("trade_time")
            if not isinstance(raw_time, str) or not raw_time.strip():
                continue
            try:
                when = datetime.fromisoformat(raw_time.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.utcoffset() is None:
                continue
            when = when.astimezone(_SHANGHAI_TZ)
            fill = {
                "account_id": ACCOUNT_ID,
                "symbol": SYMBOL,
                "side": side,
                "quantity": quantity,
                "price": price,
                "trade_date": when.date().isoformat(),
                "trade_time": when.strftime("%H:%M:%S"),
                "apply_fees": True,
                "note": f"User-confirmed fill reconciled from project record {record.get('id', 'unknown')}",
                "source_record_id": record.get("id"),
            }
            key = _fill_key(fill)
            if key in seen:
                continue
            seen.add(key)
            fills.append(fill)
    fills.sort(key=lambda item: (item["trade_date"], item["trade_time"], item["side"], item["price"]))
    return fills


def _ledger_fill_keys(handler: Any) -> set[tuple[str, str, int, float]]:
    events = handler.store.list_events(
        {"event_type": "trade_filled", "account_id": ACCOUNT_ID, "symbol": SYMBOL, "limit": 100000}
    )
    keys: set[tuple[str, str, int, float]] = set()
    for event in events:
        timestamp = str(event.get("timestamp") or "")
        metadata = event.get("metadata") or {}
        side = str(metadata.get("side") or event.get("side") or "").upper()
        if len(timestamp) < 19 or side not in {"BUY", "SELL"}:
            continue
        try:
            keys.add((
                timestamp[:19],
                side,
                int(event.get("quantity") or 0),
                round(float(event.get("price") or 0), 8),
            ))
        except (TypeError, ValueError):
            continue
    return keys


def grid_fill_reconciliation(handler: Any, memory: Any) -> dict[str, Any]:
    """Read-only comparison between confirmed research fills and the trade ledger."""
    fills = confirmed_grid_fills_from_records(memory.current())
    ledger_keys = _ledger_fill_keys(handler)
    missing = [fill for fill in fills if _fill_key(fill) not in ledger_keys]
    return {
        "confirmed_fill_count": len(fills),
        "matched_fill_count": len(fills) - len(missing),
        "missing_fill_count": len(missing),
        "missing_fills": [
            {
                "source_record_id": fill.get("source_record_id"),
                "side": fill["side"],
                "quantity": fill["quantity"],
                "price": fill["price"],
                "trade_date": fill["trade_date"],
                "trade_time": fill["trade_time"],
            }
            for fill in missing
        ],
    }


def reconcile_confirmed_grid_fills(handler: Any, memory: Any) -> dict[str, Any]:
    """Backfill only missing, explicitly user-confirmed fills, preserving audit history."""
    state = grid_fill_reconciliation(handler, memory)
    missing = confirmed_grid_fills_from_records(memory.current())
    ledger_keys = _ledger_fill_keys(handler)
    missing = [fill for fill in missing if _fill_key(fill) not in ledger_keys]
    if not missing:
        return {**state, "repaired_count": 0, "errors": [], "missing_after": 0}

    trading = handler.trading
    original_price_guard = getattr(trading, "_guard_price_sanity", None)
    if original_price_guard is not None:
        trading._guard_price_sanity = lambda *args, **kwargs: None
    repaired: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    try:
        for fill in missing:
            payload = {key: value for key, value in fill.items() if key != "source_record_id"}
            try:
                result = trading.backfill_trade(payload)
            except Exception as exc:
                errors.append({
                    "source_record_id": str(fill.get("source_record_id") or ""),
                    "error": str(exc),
                })
                continue
            repaired.append({
                "source_record_id": fill.get("source_record_id"),
                "side": result["side"],
                "quantity": result["quantity"],
                "price": result["price"],
                "timestamp": result["timestamp"],
            })
            ledger_keys.add(_fill_key(fill))
    finally:
        if original_price_guard is not None:
            trading._guard_price_sanity = original_price_guard

    after = grid_fill_reconciliation(handler, memory)
    return {
        **after,
        "missing_before": len(missing),
        "repaired_count": len(repaired),
        "repaired": repaired,
        "errors": errors,
        "missing_after": after["missing_fill_count"],
    }


def _latest_grid_config(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(records):
        if record.get("kind") != "strategy":
            continue
        content = record.get("content") or {}
        candidate = content.get("grid_config", content)
        if isinstance(candidate, dict) and any(field in candidate for field in GRID_FIELDS):
            return candidate
    return None


def evaluate_grid(
    handler: Any,
    memory: Any,
    grid_config: dict[str, Any] | None = None,
    *,
    quote_getter: Callable[[str], dict[str, Any]] = cn_market.get_quote,
    kline_getter: Callable[..., dict[str, Any]] = cn_market.get_kline,
) -> dict[str, Any]:
    """Return live account metrics and deterministic grid guardrail signals."""
    account = handler.trading.get_account(ACCOUNT_ID)
    if not account:
        raise ValueError(f"unknown paper account: {ACCOUNT_ID}")
    quote = quote_getter(CODE)
    price = float(quote["price"])
    positions = handler.trading.list_positions(ACCOUNT_ID)
    position = next((item for item in positions if item.get("symbol") == SYMBOL), None) or {}
    quantity = int(position.get("quantity") or 0)
    avg_cost = float(position.get("avg_cost") or 0)
    cash = round(float(account["cash"]), 2)
    market_value = round(quantity * price, 2)
    equity = round(cash + market_value, 2)
    metrics: dict[str, Any] = {
        "cash": cash,
        "quantity": quantity,
        "avg_cost": avg_cost,
        "market_price": price,
        "market_value": market_value,
        "equity": equity,
        "unrealized_pnl": round(quantity * (price - avg_cost), 2),
        "exposure_pct": round(market_value / equity * 100, 2) if equity else None,
    }

    history: dict[str, Any] = {"available": False}
    try:
        bars = kline_getter(CODE, period="daily", limit=60).get("bars") or []
        recent = bars[-20:]
        if recent:
            closes = [float(row["close"]) for row in recent if row.get("close") is not None]
            lows = [float(row["low"]) for row in recent if row.get("low") is not None]
            highs = [float(row["high"]) for row in recent if row.get("high") is not None]
            history = {
                "available": bool(closes and lows and highs),
                "sessions": len(recent),
                "low": min(lows) if lows else None,
                "high": max(highs) if highs else None,
                "mean_close": round(sum(closes) / len(closes), 4) if closes else None,
            }
    except Exception as exc:  # live quote remains useful if history fails
        history = {"available": False, "error": str(exc)}

    config = grid_config or _latest_grid_config(memory.current())
    missing = [field for field in GRID_FIELDS if not isinstance(config, dict) or config.get(field) in (None, "")]
    base = {
        "account_id": ACCOUNT_ID,
        "symbol": SYMBOL,
        "quote": quote,
        "metrics": metrics,
        "recent_20_sessions": history,
        "grid_config": config,
        "missing_config_fields": missing,
        "execution": "analysis_only_no_trade_or_strategy_change",
    }
    if missing:
        return {**base, "status": "needs_grid_config", "signals": ["confirm_and_record_grid_config"]}

    lower = float(config["lower_price"])
    upper = float(config["upper_price"])
    reference = float(config["reference_price"])
    spacing_pct = float(config["spacing_pct"])
    order_quantity = int(config["order_quantity"])
    min_position = int(config["min_position"])
    max_position = int(config["max_position"])
    if not (0 < lower < upper and lower <= reference <= upper):
        raise ValueError("grid prices must satisfy 0 < lower_price <= reference_price <= upper_price")
    if spacing_pct <= 0 or order_quantity <= 0 or min_position < 0 or max_position < min_position:
        raise ValueError("invalid spacing, order quantity, or position limits")

    next_buy = round(reference * (1 - spacing_pct / 100), 4)
    next_sell = round(reference * (1 + spacing_pct / 100), 4)
    commission_rate = float(account.get("commission_rate") or 0)
    estimated_buy_cash = round(order_quantity * price * (1 + commission_rate), 2)
    sellable_above_base = max(quantity - min_position, 0)
    capacity = max(max_position - quantity, 0)
    signals: list[str] = []
    if price < lower or price > upper:
        signals.append("pause_and_review_price_outside_range")
    if quantity >= max_position:
        signals.append("max_position_reached_no_more_buys")
    if sellable_above_base < order_quantity:
        signals.append("minimum_base_blocks_full_sell")
    if price <= next_buy:
        signals.append("buy_level_reached" if cash >= estimated_buy_cash and capacity >= order_quantity else "buy_level_reached_but_guardrail_blocks")
    if price >= next_sell:
        signals.append("sell_level_reached" if sellable_above_base >= order_quantity else "sell_level_reached_but_guardrail_blocks")
    grid_steps = (price / reference - 1) / (spacing_pct / 100)
    if abs(grid_steps) >= 2:
        signals.append("review_reference_price_drift")
    if history.get("available") and (history["low"] < lower or history["high"] > upper):
        signals.append("review_range_against_recent_20_sessions")
    if not signals:
        signals.append("within_configured_guardrails")

    return {
        **base,
        "status": "evaluated",
        "computed": {
            "next_buy_price": next_buy,
            "next_sell_price": next_sell,
            "grid_steps_from_reference": round(grid_steps, 2),
            "range_position_pct": round((price - lower) / (upper - lower) * 100, 2),
            "estimated_cash_for_one_buy": estimated_buy_cash,
            "can_buy_one_grid": cash >= estimated_buy_cash and capacity >= order_quantity,
            "can_sell_one_grid": sellable_above_base >= order_quantity,
            "position_capacity": capacity,
            "sellable_above_minimum": sellable_above_base,
        },
        "signals": signals,
    }
