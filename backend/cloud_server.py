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


if __name__ == "__main__":
    server.run()
