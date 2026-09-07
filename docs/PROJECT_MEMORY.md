# Project memory API v1

This adds a research log alongside the existing trading ledger. It does not install
Hermes, create trades, evaluate strategies, schedule jobs, or promote skills.

## Storage and rollout

`backend.cloud_server` installs the routes using `server.DB_PATH`. Only one new
SQLite table, `project_records`, is created; existing tables and ledger rows are
untouched by this module. With the current Docker WORKDIR `/app` and no path
overrides, `backend.paths.db_path()` resolves to `/app/data/audit.sqlite3`, inside
the configured Railway volume. Confirm `/api/health` in the running service before
claiming this path is verified. Do not set `PAPER_TRADING_HOME=/app/data` to fix it:
that would move the database to `/app/data/data/audit.sqlite3`.

Existing cloud startup code bootstraps a grid account and history and updates fee
settings. This PR does not change those existing startup behaviors. Before rollout,
verify current ledger state and backup availability. A local reopen test does not
prove persistence across a Railway redeployment.

## Authentication

Every new route requires `X-Admin-Token` equal to the Railway environment variable
`PAPER_TRADING_API_TOKEN`, including loopback requests. Missing configuration returns
503; missing/incorrect credentials return 401. Never place credentials in project
source files, query strings, or GitHub. Existing routes retain their prior policy.
The pre-existing public `/api/grid/588000` route is outside this change.

## Endpoints

- `GET /api/project/context`: grid paper account, positions, most recent 50 trade
  events, all unsuperseded research records, quality flags, and capability flags.
  No market quote is fetched; stored account/position values are not live marks.
  Historical screenshot provenance is explicitly not reverified by this module.
  Research `strategy` records are notes, not the trading engine's active settings.
- `GET /api/project/records?after=0&limit=100`: append order, cursor pagination;
  read until `has_more=false`. Maximum page size 500.
- `POST /api/project/records`: append a record. 201 new / 200 identical retry /
  409 conflicting key or stale correction / 400 invalid payload / 413 oversized.

Example request body (fictional test note, not a trade):

```json
{
  "kind": "memory",
  "idempotency_key": "setup-readback-test-v1",
  "source": "manual-setup-test",
  "event_time": "2026-09-07T17:30:00+08:00",
  "verification": "unverified",
  "content": {"text": "Setup test record; not an investment fact."}
}
```

Kinds: `memory`, `strategy`, `decision`, `review`, `learning`. Content must be a
nonempty JSON object; source, timezone-aware event time and idempotency key are
required. Verification is provenance supplied by the authenticated caller, not a
server claim of independent fact checking. Allowed labels: `unverified`,
`user_confirmed`, `source_checked`. All learning records get server-controlled
`learning_status=candidate`, regardless of free-text content.

Review must reference a decision via `related_ids`; learning must reference a
review. References must exist. To correct a record, append a new same-kind record
with `supersedes` and `expected_version`. Old records remain readable; a second
correction of an already superseded parent is rejected. Never use an existing key
for different content. Keys are global within this single investment project.

For decisions, content should contain evidence, proposal, expected outcome,
invalidation conditions and review time. For reviews, include actual execution,
observed result and unchanged-strategy comparison. For learning, record applicable
conditions, evidence, counterexamples and a proposed independent evaluation. These
content conventions are not an automated evaluation engine.

## ChatGPT connection is a separate step

These are authenticated HTTP endpoints, not an MCP server. GitHub/Railway management
plugins do not automatically expose their business data. Next connect an authorized
HTTP client or an MCP wrapper with explicit read/write tools. New conversations must
actually call the data tool; a URL in a project source does not establish a connection.
Do not label the integration complete before testing write/read in a fresh chat.

## Verification

Run `python3 -m unittest discover -s tests -p test_project_memory.py -v`.
Covers concurrent idempotency, reopen durability, correction history, link validation,
candidate-only learning, cursor pagination, invalid values, HTTP authentication,
HTTP readback, and unchanged fallback routes. Actual cloud handler integration was
also checked in a temporary empty data directory without invoking startup bootstrap.

Pending production acceptance: authenticated write/read; redeployment readback;
fresh-chat tool readback; backup/restore. Screenshot import, dynamic quote quality,
automatic evaluation and skill version promotion remain future work.
