# ChatGPT project MCP connection

## What this adds

An owner-only FastMCP 3.4.7 server, using the library's GitHub OAuth provider.
Five tools: `get_project_context`, `list_project_records`,
`append_project_record`, `record_confirmed_fill`, and `evaluate_grid`.
The fill tool only appends an explicitly confirmed fill to the fixed 588000 paper
account; it cannot reach a live broker. The evaluation tool fetches delayed market
data and returns guardrail signals without trading or changing the strategy.
No repo-editing, deployment or automatic skill-promotion tool is exposed.

The existing HTTP service proxies only MCP/OAuth paths to a loopback-only ASGI
worker in the same container. Other API routes retain their original authentication
and behavior. MCP uses stateless Streamable HTTP with JSON responses. GET /mcp
returns 405 deliberately; it is not a browser page or SSE subscription.

## 1. Merge and deploy

The existing Docker command remains `python -m backend.cloud_server`. When OAuth
variables are absent, the old app starts normally; MCP routes return 503 and are
never opened anonymously. Adding all four variables activates the MCP worker on
the next deployment. An invalid complete configuration fails startup rather than
silently exposing data. Keep one service replica / one process for file storage.

## 2. Create a GitHub OAuth App

Use https://github.com/settings/applications/new while signed in as the intended
owner. This is an OAuth App, not a GitHub App or a personal access token.

| Field | Value |
| --- | --- |
| Application name | Simulation Investing Agent |
| Homepage URL | https://paper-trading-production-a050.up.railway.app |
| Authorization callback URL | https://paper-trading-production-a050.up.railway.app/auth/callback |

Generate a Client Secret, then save it directly in Railway variables. Never paste
it into chat, commit it, or put it in a URL. The OAuth provider requests `read:user`
only, not repository write access. It uses GitHub identity to authorize access to
this separate research database. Only the configured numeric GitHub account ID
passes resource authorization, with an additional check inside each tool.

## 3. Configure Railway service variables

| Variable | Value |
| --- | --- |
| MCP_BASE_URL | https://paper-trading-production-a050.up.railway.app |
| MCP_GITHUB_USER_ID | 94347935 (GodivaMM1, verified in connected GitHub metadata) |
| MCP_GITHUB_CLIENT_ID | Client ID from the new GitHub OAuth App |
| MCP_GITHUB_CLIENT_SECRET | Client Secret from that app; secret value |

The existing `PAPER_TRADING_API_TOKEN` remains unchanged and is not given to the
MCP client. These OAuth credentials are new, separate from the connected GitHub
management plugin. Saving service variables can trigger a deployment; configure
them together where possible. Do not change the data directory.

Signing and encryption keys are created with restrictive permissions in
`data/mcp_oauth/`; OAuth state uses encrypted FileTreeStore with sanitized keys.
This folder is excluded from git and, under the current layout, lives on the
Railway `/app/data` volume. Keys and state survive recreation in local tests.
Treat volume backups as secrets: the keys and encrypted state are co-located, so
encryption does not protect against a reader of the entire volume or its backup.
Back up the whole folder consistently; rotating signing/encryption keys invalidates
sessions and may require reauthorization. Production restore has not been tested.

## 4. Connect ChatGPT

Enable Developer mode in ChatGPT on the web. Create a developer-mode app:

- Name: 模拟投资研究账本
- MCP URL: https://paper-trading-production-a050.up.railway.app/mcp
- Authentication: OAuth
- Client registration: dynamic (DCR); leave optional preconfigured client fields
  blank. The GitHub OAuth App credentials belong in Railway, not these client fields.

Approve the MCP consent page, then log into GitHub as GodivaMM1 and authorize the
OAuth App. Only exact approved ChatGPT callback patterns are allowed by the server.
An unrelated redirect URI is rejected. If the product shows a different callback,
inspect it before changing the allowlist; do not allow all callback domains.

## 5. Acceptance

Discover exactly the five tools. Read context without creating trades. Write a
clearly labeled non-financial setup-test note with a unique idempotency key. Read
the returned ID; retry the identical write and confirm `created=false`. Verify in
a fresh chat and again after a controlled redeployment. These production checks
require the real account authorization and have not yet been performed.

For fill acceptance, extract screenshot fields first, show them to the user, and
call `record_confirmed_fill` only after explicit confirmation. Use a deterministic
idempotency key. The server rejects an obvious existing fill and locks failed or
ambiguous requests for inspection. For grid evaluation, first store one confirmed
`strategy` record with `content.grid_config` containing: `lower_price`,
`upper_price`, `reference_price`, `spacing_pct`, `order_quantity`, `min_position`,
and `max_position`. Missing values are reported rather than invented.

Test results before submission: 7 local MCP/auth/proxy tests pass, including owner
denial and read/write with mocked upstream identity, metadata discovery, redirect
rejection, consent and client registration after provider recreation, persisted
keys, actual loopback proxy, and legacy route fallback. These tests do not prove
the real GitHub login or live ChatGPT linking flow. Existing 5 record tests and 4
path tests should also remain passing.

References: https://developers.openai.com/plugins/build/auth and
https://gofastmcp.com/integrations/github .
