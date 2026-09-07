from __future__ import annotations

import os
from urllib.parse import parse_qs, unquote, urlparse

from backend import admin_link
from backend import cn_market
from backend import server

_original_node_token = admin_link.node_token


def _cloud_node_token() -> str:
    configured = os.environ.get("PAPER_TRADING_API_TOKEN", "").strip()
    return configured or _original_node_token()


admin_link.node_token = _cloud_node_token

_original_guard_remote = server.AuditRequestHandler._guard_remote


def _cloud_guard_remote(self) -> bool:
    path = urlparse(self.path).path
    if path == "/api/health":
        return True
    if path.startswith("/api/quote/") or path.startswith("/api/kline/") or path == "/api/quotes":
        return True
    if path == "/api/grid/588000":
        return True
    return _original_guard_remote(self)


server.AuditRequestHandler._guard_remote = _cloud_guard_remote

_original_do_get = server.AuditRequestHandler.do_GET

GRID_ACCOUNT_ID = "acct_588000_grid"
GRID_SYMBOL = "588000.SH"


def _live_grid_summary() -> dict:
    """Return the 588000 paper account marked to the latest delayed A-share quote."""
    quote = cn_market.get_quote("588000")
    price = float(quote["price"])
    trading = server.AuditRequestHandler.trading
    account = trading.get_account(GRID_ACCOUNT_ID)
    if not account:
        raise ValueError(f"unknown account_id: {GRID_ACCOUNT_ID}")
    positions = trading.list_positions(GRID_ACCOUNT_ID)
    position = next((p for p in positions if p["symbol"] == GRID_SYMBOL), None)
    quantity = int(position["quantity"]) if position else 0
    avg_cost = float(position["avg_cost"]) if position else 0.0
    market_value = round(quantity * price, 2)
    unrealized_pnl = round(quantity * (price - avg_cost), 2)
    unrealized_pct = round((price / avg_cost - 1) * 100, 2) if avg_cost else 0.0
    cash = round(float(account["cash"]), 2)
    total_equity = round(cash + market_value, 2)
    total_pnl = round(total_equity - float(account["initial_cash"]), 2)
    total_return_pct = round(total_pnl / float(account["initial_cash"]) * 100, 2)
    return {
        "accountId": GRID_ACCOUNT_ID,
        "initialCash": float(account["initial_cash"]),
        "cash": cash,
        "position": {
            "symbol": GRID_SYMBOL,
            "name": quote.get("name"),
            "quantity": quantity,
            "avgCost": avg_cost,
            "price": price,
            "marketValue": market_value,
            "unrealizedPnl": unrealized_pnl,
            "unrealizedPct": unrealized_pct,
        },
        "totalEquity": total_equity,
        "totalPnl": total_pnl,
        "totalReturnPct": total_return_pct,
        "quote": quote,
        "mark": {"source": quote.get("source"), "retrievedAt": quote.get("retrievedAt")},
    }


def _cloud_do_get(self) -> None:
    parsed = urlparse(self.path)
    path = parsed.path
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    try:
        if path == "/api/grid/588000":
            self._json(_live_grid_summary())
            return
        if path.startswith("/api/quote/"):
            code = unquote(path.removeprefix("/api/quote/").strip("/"))
            self._json(cn_market.get_quote(code))
            return
        if path == "/api/quotes" and query.get("codes") is not None:
            codes = [item.strip() for item in query.get("codes", "").split(",") if item.strip()]
            self._json({"quotes": cn_market.get_quotes(codes)})
            return
        if path.startswith("/api/kline/"):
            code = unquote(path.removeprefix("/api/kline/").strip("/"))
            period = query.get("period", "daily")
            limit = int(query.get("limit", "100"))
            self._json(cn_market.get_kline(code, period=period, limit=limit))
            return
    except ValueError as exc:
        from http import HTTPStatus
        self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return
    except Exception as exc:
        from http import HTTPStatus
        self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        return
    _original_do_get(self)


server.AuditRequestHandler.do_GET = _cloud_do_get

# Project records share the existing persistent database; no trades are created here.
from backend.project_api import install as install_project_api
install_project_api(server.AuditRequestHandler, server.DB_PATH)


def _bootstrap_grid_account() -> None:
    trading = server.AuditRequestHandler.trading
    account = trading.get_account(GRID_ACCOUNT_ID)
    if not account:
        trading.create_account({
            "id": GRID_ACCOUNT_ID,
            "name": "588000 Grid",
            "owner": "588000-grid",
            "initial_cash": 40000.0,
            "currency": "CNY",
            "market": "CN_A",
            "commission_rate": 0.0003,
            "min_commission": 0.0,
            "stamp_duty_rate": 0.0,
            "auto_reverse_repo_enabled": False,
        })
        print(f"Bootstrapped paper account {GRID_ACCOUNT_ID} with CNY 40000", flush=True)
    trading.update_account(GRID_ACCOUNT_ID, {
        "commission_rate": 0.0003,
        "min_commission": 0.0,
        "stamp_duty_rate": 0.0,
        "auto_reverse_repo_enabled": False,
    })


def _bootstrap_confirmed_grid_history() -> None:
    trading = server.AuditRequestHandler.trading
    audit = server.AuditRequestHandler.store
    fills = [
        {"account_id": GRID_ACCOUNT_ID, "symbol": GRID_SYMBOL, "side": "BUY", "quantity": 11700, "price": 1.708, "trade_date": "2026-09-03", "trade_time": "09:52:55", "apply_fees": True, "note": "Initial/base position from confirmed source screenshot"},
        {"account_id": GRID_ACCOUNT_ID, "symbol": GRID_SYMBOL, "side": "BUY", "quantity": 1700, "price": 1.668, "trade_date": "2026-09-04", "trade_time": "14:29:46", "apply_fees": True, "note": "Grid add from confirmed source screenshot"},
        {"account_id": GRID_ACCOUNT_ID, "symbol": GRID_SYMBOL, "side": "SELL", "quantity": 1700, "price": 1.708, "trade_date": "2026-09-07", "trade_time": "13:30:00", "apply_fees": True, "note": "Grid sell from confirmed source screenshot; sell fee estimated from 0.03% account rate because screenshot shows -- for today"},
    ]
    existing = audit.list_events({"event_type": "trade_filled", "account_id": GRID_ACCOUNT_ID, "symbol": GRID_SYMBOL, "limit": 100000})

    def _already_present(fill: dict) -> bool:
        target_day = fill["trade_date"]
        target_side = fill["side"]
        target_qty = int(fill["quantity"])
        target_price = float(fill["price"])
        for event in existing:
            metadata = event.get("metadata") or {}
            if (
                str(event.get("timestamp", ""))[:10] == target_day
                and str(metadata.get("side", "")).upper() == target_side
                and int(event.get("quantity") or 0) == target_qty
                and abs(float(event.get("price") or 0.0) - target_price) < 1e-9
            ):
                return True
        return False

    missing = [fill for fill in fills if not _already_present(fill)]
    if not missing:
        print(f"588000 confirmed history complete ({len(existing)} fills); bootstrap skipped", flush=True)
        return

    # These are user-confirmed broker screenshot fills. Avoid connector-dependent
    # price sanity probes during startup; the screenshot itself is the source of truth.
    original_price_guard = trading._guard_price_sanity
    trading._guard_price_sanity = lambda *args, **kwargs: None
    try:
        results = []
        for fill in missing:
            result = trading.backfill_trade(fill)
            results.append(result)
            existing.append({
                "timestamp": result["timestamp"],
                "quantity": result["quantity"],
                "price": result["price"],
                "metadata": {"side": result["side"]},
            })
        print("Repaired confirmed 588000 history: " + "; ".join(f"{r['side']} {r['quantity']}@{r['price']}" for r in results), flush=True)
    finally:
        trading._guard_price_sanity = original_price_guard


if __name__ == "__main__":
    _bootstrap_grid_account()
    _bootstrap_confirmed_grid_history()
    server.run()
