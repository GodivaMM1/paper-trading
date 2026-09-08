"""Owner-only MCP adapter. OAuth is implemented by FastMCP's GitHub provider."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.exceptions import ToolError
from cryptography.fernet import Fernet
from key_value.aio.stores.filetree import (
    FileTreeStore, FileTreeV1KeySanitizationStrategy,
    FileTreeV1CollectionSanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from backend.project_memory import ProjectMemory
from backend.project_api import project_context
from backend.project_grid import ConfirmedFillRecorder, evaluate_grid as evaluate_grid_state


class OwnerGitHubProvider(GitHubProvider):
    def __init__(self, *, owner_id: str, **kwargs):
        self.owner_id = owner_id
        super().__init__(**kwargs)

    async def load_access_token(self, token):
        access = await super().load_access_token(token)
        if access and str(access.claims.get('sub', '')) == self.owner_id:
            return access
        return None


def persistent_secret(path: Path, generator):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    value = generator()
    with os.fdopen(fd, 'w') as f:
        f.write(value)
    return value


def build_mcp(handler, db_path):
    base_url = os.environ['MCP_BASE_URL'].rstrip('/')
    url = urlparse(base_url)
    if url.scheme != 'https' or not url.netloc or url.path or url.query or url.fragment or url.username:
        raise ValueError('MCP_BASE_URL must be an HTTPS origin')
    owner = os.environ['MCP_GITHUB_USER_ID'].strip()
    if not owner.isdigit():
        raise ValueError('MCP_GITHUB_USER_ID must be a numeric GitHub account ID')
    private = Path(db_path).parent / 'mcp_oauth'
    signing = persistent_secret(private / 'signing.secret', lambda: secrets.token_urlsafe(48))
    encryption = persistent_secret(private / 'encryption.secret', lambda: Fernet.generate_key().decode())
    directory = private / 'state'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage = FernetEncryptionWrapper(
        key_value=FileTreeStore(
            data_directory=directory,
            key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
            collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
        ),
        fernet=Fernet(encryption.encode()),
    )
    auth = OwnerGitHubProvider(
        owner_id=owner,
        client_id=os.environ['MCP_GITHUB_CLIENT_ID'],
        client_secret=os.environ['MCP_GITHUB_CLIENT_SECRET'],
        base_url=base_url,
        required_scopes=['read:user'],
        redirect_path='/auth/callback',
        allowed_client_redirect_uris=[
            'https://chatgpt.com/connector_platform_oauth_redirect',
            'https://chatgpt.com/connector/oauth/*',
        ],
        jwt_signing_key=signing, client_storage=storage,
        # GitHub already presents the owner with an authorization screen, and
        # every MCP tool independently enforces the numeric owner id. Avoid a
        # second cookie-based consent form, which is fragile across mobile and
        # desktop browser handoffs.
        require_authorization_consent=False,
        fastmcp_access_token_expiry_seconds=3600,
        fallback_refresh_token_expiry_seconds=30 * 24 * 3600,
        # DCR is sufficient for the first integration, avoiding dynamic URL clients.
        enable_cimd=False,
    )
    mcp = FastMCP('模拟投资研究账本', auth=auth,
                 instructions='Read project context before analysis. Record evidence and distinguish proposals from executions. Learning records are candidates, never automatic trading rules.')
    memory = ProjectMemory(db_path)
    fill_recorder = ConfirmedFillRecorder(db_path, handler)

    def require_owner():
        token = get_access_token()
        if not token or str(token.claims.get('sub', '')) != owner:
            raise ToolError('Only the configured owner may access this project.')

    @mcp.tool(annotations={'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False})
    def get_project_context() -> dict:
        """Read the grid paper account and current research notes. Prices are not live quotes."""
        require_owner()
        return project_context(handler, memory)

    @mcp.tool(annotations={'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False})
    def list_project_records(
        after: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict:
        """Read research history in append order. Follow next_after until has_more is false."""
        require_owner()
        return memory.list(after, limit)

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                           'idempotentHint': True, 'openWorldHint': False})
    def append_project_record(record: dict) -> dict:
        """Append research only; never place trades. Required fields: kind (memory/strategy/decision/review/learning), content object, source, timezone-aware event_time, unique idempotency_key. Optional: verification (unverified/user_confirmed/source_checked), related_ids, supersedes and expected_version. Review must link a decision; learning must link a review and remains a candidate. Retry identical content using the same key. Corrections preserve the old record."""
        require_owner()
        try:
            saved, created = memory.append(record)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {'record': saved, 'created': created}

    @mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                           'idempotentHint': True, 'openWorldHint': False})
    def record_confirmed_fill(fill: dict) -> dict:
        """Record one explicitly user-confirmed 588000.SH paper fill and update cash/position. Never call from an unconfirmed screenshot extraction. Required: user_confirmed=true, side BUY/SELL, positive quantity and price, trade_date YYYY-MM-DD, source, and a deterministic idempotency_key. Optional: trade_time HH:MM:SS, apply_fees (default true), note. The target is fixed to acct_588000_grid and this cannot place a live brokerage order."""
        require_owner()
        try:
            return fill_recorder.record(fill)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations={'readOnlyHint': True, 'destructiveHint': False,
                           'openWorldHint': True})
    def evaluate_grid(grid_config: dict | None = None) -> dict:
        """Fetch the latest delayed 588000 quote and 20-session history, combine them with the paper account, and evaluate grid guardrails. Uses the latest active strategy record's content.grid_config when no config is supplied. Required config fields are lower_price, upper_price, reference_price, spacing_pct, order_quantity, min_position, max_position. Returns signals only and never trades or changes strategy."""
        require_owner()
        try:
            return evaluate_grid_state(handler, memory, grid_config)
        except (ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc

    return mcp
