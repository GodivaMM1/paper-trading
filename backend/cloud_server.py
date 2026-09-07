from __future__ import annotations

import os
from urllib.parse import urlparse

from backend import admin_link
from backend import server


# Prefer a stable Railway-provided token for remote API access.
# If it is not configured, fall back to the project's original persisted token.
_original_node_token = admin_link.node_token


def _cloud_node_token() -> str:
    configured = os.environ.get("PAPER_TRADING_API_TOKEN", "").strip()
    return configured or _original_node_token()


admin_link.node_token = _cloud_node_token


# Keep only the health endpoint public so a browser can verify the service is alive.
_original_guard_remote = server.AuditRequestHandler._guard_remote


def _cloud_guard_remote(self) -> bool:
    if urlparse(self.path).path == "/api/health":
        return True
    return _original_guard_remote(self)


server.AuditRequestHandler._guard_remote = _cloud_guard_remote


# Idempotent bootstrap for the dedicated 588000 grid simulation account.
# The stable account id prevents duplicate accounts on Railway redeploys.
GRID_ACCOUNT_ID = "acct_588000_grid"


def _bootstrap_grid_account() -> None:
    trading = server.AuditRequestHandler.trading
    if trading.get_account(GRID_ACCOUNT_ID):
        return
    trading.create_account(
        {
            "id": GRID_ACCOUNT_ID,
            "name": "588000 Grid",
            "owner": "588000-grid",
            "initial_cash": 40000.0,
            "currency": "CNY",
            "market": "CN_A",
            # 588000 is an ETF, so stock stamp duty should not be charged.
            "stamp_duty_rate": 0.0,
            "auto_reverse_repo_enabled": False,
        }
    )
    print(f"Bootstrapped paper account {GRID_ACCOUNT_ID} with CNY 40000", flush=True)


if __name__ == "__main__":
    _bootstrap_grid_account()
    server.run()
