from __future__ import annotations

import os
from urllib.parse import parse_qs, unquote, urlparse

from backend import admin_link
from backend import cn_market
from backend import server


# Prefer a stable Railway-provided token for remote API access.
_original_node_token = admin_link.node_token


def _cloud_node_token() -> str:
    configured = os.environ.get("PAPER_TRADING_API_TOKEN", "").strip()
    return configured or _original_node_token()


admin_link.node_token = _cloud_node_token


# Health and read-only public A-share market-data endpoints are intentionally public.
_original_guard_remote = server.AuditRequestHandler._guard_remote


def _cloud_guard_remote(self) -> bool:
    path = urlparse(self.path).path
    if path == "/api/health":
        return True
    if path.startswith("/api/quote/") or path.startswith("/api/kline/") or path == "/api/quotes":
        return True
    return _original_guard_remote(self)


server.AuditRequestHandler._guard_remote = _cloud_guard_remote


# Add simple market endpoints without changing the upstream paper-trading server module.
_original_do_get = server.AuditRequestHandler.do_GET


def _cloud_do_get(self) -> None:
    parsed = urlparse(self.path)
    path = parsed.path
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    try:
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
    except Exception as exc:  # noqa: BLE001 - market providers can fail independently
        from http import HTTPStatus
        self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        return
    _original_do_get(self)


server.AuditRequestHandler.do_GET = _cloud_do_get

GRID_ACCOUNT_ID = "acct_588000_grid"
GRID_SYMBOL = "588000.SH"


def _bootstrap_grid_account() -> None:
    trading = server.AuditRequestHandler.trading
    account = trading.get_account(GRID_ACCOUNT_ID)
    if not account:
        account = trading.create_account(
            {
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
            }
        )
        print(f"Bootstrapped paper account {GRID_ACCOUNT_ID} with CNY 40000", flush=True)

    # Match the confirmed source screenshot: 0.03% commission with no minimum;
    # 588000 is an ETF, so no stock stamp duty. This affects only future/backfilled fills.
    trading.update_account(
        GRID_ACCOUNT_ID,
        {
            "commission_rate": 0.0003,
            "min_commission": 0.0,
            "stamp_duty_rate": 0.0,
            "auto_reverse_repo_enabled": False,
        },
    )


def _bootstrap_confirmed_grid_history() -> None:
    trading = server.AuditRequestHandler.trading
    audit = server.AuditRequestHandler.store

    # Safety/idempotency: never add this bootstrap set if any valid 588000 fill already exists.
    existing = audit.list_events(
        {
            "event_type": "trade_filled",
            "account_id": GRID_ACCOUNT_ID,
            "symbol": GRID_SYMBOL,
            "limit": 100000,
        }
    )
    if existing:
        print(f"588000 history already present ({len(existing)} fills); bootstrap skipped", flush=True)
        return

    fills = [
        {
            "account_id": GRID_ACCOUNT_ID,
            "symbol": GRID_SYMBOL,
            "side": "BUY",
            "quantity": 11700,
            "price": 1.708,
            "timestamp": "2026-09-03T09:52:55+08:00",
            "apply_fees": True,
            "note": "Initial/base position from confirmed source screenshot",
        },
        {
            "account_id": GRID_ACCOUNT_ID,
            "symbol": GRID_SYMBOL,
            "side": "BUY",
            "quantity": 1700,
            "price": 1.668,
            "timestamp": "2026-09-04T14:29:46+08:00",
            "apply_fees": True,
            "note": "Grid add from confirmed source screenshot",
        },
        {
            "account_id": GRID_ACCOUNT_ID,
            "symbol": GRID_SYMBOL,
            "side": "SELL",
            "quantity": 1700,
            "price": 1.708,
            "timestamp": "2026-09-07T13:30:00+08:00",
            "apply_fees": True,
            "note": "Grid sell from confirmed source screenshot; sell fee modeled at configured 0.03% because screenshot showed pending fee",
        },
    ]

    results = [trading.backfill_trade(fill) for fill in fills]
    print(
        "Backfilled confirmed 588000 simulation history: "
        + "; ".join(
            f"{r['side']} {r['quantity']}@{r['price']} fee={r['costs']['commission']} cash={r['cash_after']} pos={r['position_after']}"
            for r in results
        ),
        flush=True,
    )


if __name__ == "__main__":
    _bootstrap_grid_account()
    _bootstrap_confirmed_grid_history()
    server.run()
