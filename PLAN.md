# PLAN: tg-mcp-bridge

Implementation order. Each step is a separate Claude Code session, a separate `step/NN-...` branch off `dev` and a separate PR into `dev`. What we build is in SPEC.md; process rules and git flow are in CLAUDE.md.

The status table is updated inside the step branch, so on `dev` a step becomes `done` only when its PR is merged.

## Status

Statuses: `todo`, `in progress`, `done`, `blocked`. The current step is the first one that is not `done`.

| Step | Title | Status |
|---|---|---|
| 1 | Project skeleton and Docker | done |
| 2 | Database | done |
| 3 | Telegram Bot API client | todo |
| 4 | Telegram Login (separate from OAuth) | todo |
| 5 | OAuth 2.1 Authorization Server + DCR | todo |
| 6 | MCP server and tools | todo |
| 7 | HTTPS deployment | todo |
| 8 | Connecting to claude.ai | todo |
| 9 | Polish | todo |
| 10 | Sending quizzes (`send_quiz`) | todo |
| 11 | Collecting answers and results (`get_quiz_results`) | todo |
| 12 | Abuse protection, alerts, privacy | todo |

---

## Step 1. Project skeleton and Docker

Do:
- Layout: `app/` (code), `tests/`, `pyproject.toml`, `uv.lock`, `.python-version`, `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.env.example`, `README.md`. `.claude/` already exists (set up by the operator) - do not modify it.
- Python project managed by uv (see SPEC.md, "Stack"): `uv init` style `pyproject.toml` with `requires-python = ">=3.14,<3.15"`, runtime deps via `uv add`, test deps via `uv add --dev`, `uv.lock` committed.
- `Dockerfile`: base `python:3.14-slim`, uv copied from the official `ghcr.io/astral-sh/uv` image, dependencies installed with `uv sync --frozen --no-dev` (lockfile is the source of truth), app runs as a non-root user.
- Starlette app with `GET /health` → `{"status":"ok"}`.
- Config loaded from env with validation (fail at startup if `BASE_URL` or `BOT_TOKEN` is missing). Tests set config via fixtures, never via env files.
- `docker-compose.yml`: service `app` (port 8000 internal only), volume `./data:/data`, `env_file: ${ENV_FILE:-.env}`. No Caddy yet.
- `.env.example` with every variable from SPEC.md: secret keys empty or `<placeholder>`, non-secret keys with harmless examples. It must pass `python .claude/hooks/env_guard.py --check .env.example` and still let the app start (placeholders are non-empty where startup validation requires a value).
- `.gitignore`: `.env`, `.env.*`, `!.env.example`, `data/`.
- CI: `.github/workflows/ci.yml` on `pull_request` to `dev` and `main` and on `push` to `dev`:
  - job `secrets`: `python .claude/hooks/env_guard.py --check .env.example`, `python .claude/hooks/env_guard.py --check-repo`, gitleaks (`gitleaks/gitleaks-action`, full history);
  - job `test`: `astral-sh/setup-uv` with Python 3.14, `uv lock --check`, `uv sync --frozen`, `uv run pytest`, `docker build`.
  - No secrets in CI (tests never call external services).
- `.github/pull_request_template.md` with the sections from CLAUDE.md ("What was done", "Validation", "Open / deferred", "Manual actions needed").

Validation:
- `ENV_FILE=.env.example docker compose up -d --build` succeeds.
- `docker compose exec app curl -s localhost:8000/health` → `{"status":"ok"}`.
- `git status --ignored` shows no `.env` in the repo; both `env_guard.py` checks pass locally.
- Starting without `BOT_TOKEN` fails with a clear error.
- `uv run pytest` passes (at least a test for `/health`); `uv lock --check` passes; `uv run python --version` shows 3.14.
- CI ran on this step's PR and is green.

## Step 2. Database

Do:
- Module `app/db.py`: create tables at startup (idempotent), access functions for each table.
- Helpers `hash_secret()`, `new_secret()`.
- Cleanup of expired `pending_auth`, `auth_codes`, `tokens`: either a background job (hourly) or on every request - pick one, but one of the two.

Validation:
- Tests: creating tables twice does not fail; CRUD for each table; expired rows are removed by cleanup.
- No raw code or token is ever stored in the database (a test checks that the hash is stored).

## Step 3. Telegram Bot API client

Do:
- `app/telegram.py`: `send_message(chat_id, text)` via `https://api.telegram.org/bot<TOKEN>/sendMessage`, plain text without `parse_mode`.
- Split text into chunks of up to 4000 characters, on line boundaries where possible.
- Error handling: 403 → `BotBlocked` exception; 429 → one retry after `retry_after`; anything else → exception with a description.
- Script `uv run python -m app.cli send <tg_id> "<text>"` for manual checks (inside the container: `python -m app.cli ...`).

Validation:
- respx tests: short text = 1 request; 9000-character text = 3 requests in the right order; 403 → `BotBlocked`; 429 → retry.
- Manual (done by the operator, needs the real token; Claude Code only gives the command): after the owner sends /start to the bot, `uv run python -m app.cli send <owner tg_id> "test"` delivers the message.

## Step 4. Telegram Login (separate from OAuth)

Do:
- `GET /login?pid=<id>`: HTML page with the Telegram Login Widget:
  ```html
  <script async src="https://telegram.org/js/telegram-widget.js?22"
    data-telegram-login="{BOT_USERNAME}" data-size="large"
    data-request-access="write"
    data-auth-url="{BASE_URL}/tg-callback?pid={pid}"></script>
  ```
  If `pid` is unknown or expired - an error page.
- `app/tglogin.py`: `verify_telegram_auth(params) -> dict | None`:
  - exclude `hash` and `pid` from the check;
  - `data_check_string` = sorted `key=value` pairs joined with `\n`;
  - `secret = sha256(BOT_TOKEN)`, expected hash = `hmac_sha256(secret, data_check_string).hexdigest()`;
  - compare with `hmac.compare_digest`;
  - `auth_date` no older than 10 minutes.
- Temporary debug endpoint `GET /tg-callback` for this step: on a valid signature, upsert into `users` and show the `tg_id`. It is replaced in step 5.

Validation:
- Tests: valid data (signed with a test token) passes; a modified field → rejected; old `auth_date` → rejected; an extra `pid` does not break the check.
- A manual check is only possible after step 7 (the widget needs the domain set in BotFather). Mark it as deferred in the report.

## Step 5. OAuth 2.1 Authorization Server + DCR

Do (via the `mcp.server.auth` provider, if the SDK supports it):
- `GET /.well-known/oauth-authorization-server` (at the domain root): `issuer`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `revocation_endpoint`, `response_types_supported: ["code"]`, `grant_types_supported: ["authorization_code","refresh_token"]`, `code_challenge_methods_supported: ["S256"]`, `token_endpoint_auth_methods_supported: ["none"]`.
- `GET /.well-known/oauth-protected-resource` (and the variant with the `/mcp` suffix): `resource: {BASE_URL}/mcp`, `authorization_servers: [BASE_URL]`.
- `POST /register` (DCR, RFC 7591): accepts only `redirect_uris` from `ALLOWED_REDIRECT_URIS`, otherwise 400 `invalid_redirect_uri`. Public client (no secret).
- `GET /authorize`: validates `client_id`, `redirect_uri`, `response_type=code`, `code_challenge` + `S256`; stores `pending_auth`; 302 to `/login?pid=...`.
- `GET /tg-callback` (production): verifies the signature (step 4), loads `pending_auth`, upserts the user, creates a one-time `code`, deletes `pending_auth`, 302 to `redirect_uri?code=...&state=...`.
- `POST /token`:
  - `authorization_code`: the code exists, is unused, not expired, `client_id` and `redirect_uri` match, PKCE is valid → access + refresh tokens. Code reuse → revoke all tokens issued from it.
  - `refresh_token`: rotation (the old refresh token is revoked, a new pair is issued).
- `POST /revoke`.

Validation:
- `curl` on both well-known endpoints returns valid JSON.
- Integration test of the whole flow without a browser: register → authorize (302 to /login) → tg-callback with signed test data (302 to redirect_uri with code and state) → token with the correct `code_verifier` → tokens issued.
- Negative tests: foreign redirect_uri in DCR; wrong `code_verifier`; reused code; expired code; refresh with an old (already rotated) token.

## Step 6. MCP server and tools

Do:
- MCP over Streamable HTTP at `/mcp`, protected by the bearer token from step 5.
- No token or an invalid token: 401 with the header `WWW-Authenticate: Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"`.
- Check that the token was issued for this `resource` (audience).
- Tools:
  - `send_telegram(text: str)` - description: "Send a message to the current user's Telegram. Use when the user asks to notify them, send results, a summary or a report to Telegram." Returns `sent (N parts)`. On `BotBlocked` - sets `users.blocked_bot = true` and returns a clear error: "User blocked the bot, ask them to unblock it in Telegram".
  - `whoami()` - returns the current user's Telegram username (for debugging).
- Rate limit: at most 20 `send_telegram` calls per minute per user.
- Logs: tg_id, text length, result. Never log message text.

Validation:
- `curl -i -X POST {BASE_URL}/mcp` without a token → 401 with the correct `WWW-Authenticate`.
- Tests: `send_telegram` with a valid token sends to the right `chat_id` (respx); foreign/expired token → 401; rate limit triggers on the 21st call.
- Isolation tests (two users A and B, each with their own token, including tokens issued to different OAuth clients):
  - `send_telegram` with A's token sends only to A's `chat_id`, never a single request to B's `chat_id`;
  - tools have no parameter that could name another recipient (a test on the tool schemas: `inputSchema` has no `chat_id`, `tg_id`, `user_id` or similar fields);
  - `whoami` with A's token returns A, with B's token - B;
  - rate limits are counted separately: A hitting the limit does not block B;
  - revoking A's token (`/revoke`) does not affect B's token;
  - A's refresh token cannot be exchanged on behalf of OAuth client B.
- MCP Inspector (`npx @modelcontextprotocol/inspector`) locally against `http://localhost:8000/mcp` sees the tools (if OAuth in Inspector does not work locally because of the widget - note it and verify in step 7).

## Step 7. HTTPS deployment

Do:
- Add Caddy to `docker-compose.yml`: `{$DOMAIN} { reverse_proxy app:8000 }`, volumes for certificates, ports 80 and 443.
- Document the owner's manual steps in the README:
  - Namecheap → Domain List → wentthrough.com → Advanced DNS → A Record, Host `notify`, Value = server IP;
  - BotFather → `/setdomain` → `notify.wentthrough.com`;
  - on the server: `cp .env.example .env`, fill in real values, `chmod 600 .env`.
- Before starting Caddy, check that `dig +short notify.wentthrough.com` returns the server IP, otherwise Caddy will not get a certificate.
- Make sure the app works correctly behind the proxy (the https scheme in generated URLs comes from `BASE_URL`, not from the request).

Validation:
- From an external machine, `curl https://notify.wentthrough.com/health` and both well-known endpoints respond, the certificate is valid.
- Manual check for step 4: run the authorize flow through MCP Inspector, sign in with Telegram, the flow reaches the token.
- MCP Inspector against `https://notify.wentthrough.com/mcp`: full OAuth works, `whoami` returns the owner's username, `send_telegram("test")` delivers the message.

## Step 8. Connecting to claude.ai

Do:
- README section: claude.ai → Settings → Connectors → Add custom connector → URL `https://notify.wentthrough.com/mcp`, leave OAuth client id/secret empty.

Validation (manual):
- The connector is added, Telegram sign-in works, the connector shows as connected.
- In a chat: "send me a test message in Telegram" → the message arrives.
- A scheduled task whose prompt ends with "at the end, send the result via send_telegram" → the message arrives after the run.
- If claude.ai fails on the callback while Inspector works: check issues at github.com/anthropics/claude-ai-mcp and the server logs, record at which step the flow broke.

## Step 9. Polish

Do:
- The bot replies to `/start` with a short text: "This bot delivers messages from Claude. To connect: <BASE_URL>". Updates come via webhook `POST /tg-webhook` (not long polling - step 11 depends on it), registered with `setWebhook` and `secret_token` at app startup.
- SQLite backup (daily copy on a volume).
- `README.md`: architecture, env, running, how to connect, troubleshooting.

Validation:
- `/start` in the bot gets a reply.
- All tests are green, `docker compose up -d --build` from scratch brings up a working service.

## Step 10. Sending quizzes (`send_quiz`)

Do:
- `app/telegram.py`: `send_quiz_poll(chat_id, question, options, correct_option, explanation)` via `sendPoll` with `type=quiz`, `is_anonymous=false` (otherwise Telegram does not send `poll_answer`), `correct_option_id`, `explanation`.
- Migration: tables `quizzes` and `quiz_answers`.
- MCP tool `send_quiz`:
  - input: `topic: str`, `intro: str | None` (task text, e.g. a reading passage - sent as a regular message before the polls), `questions: list[{question, options: list[str], correct_option: int, explanation: str}]`;
  - description for Claude: "Send a practice quiz to the user's Telegram as native quiz polls. Telegram shows the user instantly whether they were right and the explanation. Use for multiple-choice practice (e.g. English tests). Put long reading passages in `intro`, not in questions.";
  - validation before sending (all errors at once, in clear text for Claude): 1-10 questions; question 1-300 characters; 2-10 options, each 1-100 characters; `correct_option` in range; explanation up to 200 characters;
  - `intro` longer than 4000 characters is split the same way as in `send_telegram`;
  - questions are sent sequentially; after each, `poll_id` and `message_id` are stored in `quizzes`;
  - returns `quiz_set_id` and the number of questions sent;
  - `send_quiz` shares the `send_telegram` rate limit (one call = one action), plus an overall limit of at most 30 polls per hour per user;
  - `BotBlocked` is handled the same way as in `send_telegram`.

Validation:
- Tests (respx): a set of 3 questions with `intro` = 1 `sendMessage` + 3 `sendPoll` in the right order, 3 rows in `quizzes` with the correct `correct_option`; `sendPoll` has `type=quiz` and `is_anonymous=false`.
- Negative tests: 11 questions, an option longer than 100 characters, `correct_option` out of range → error without a single request to Telegram.
- Isolation tests: `send_quiz` with A's token sends polls only to A's `chat_id`, rows in `quizzes` are created with A's `tg_id`; the 30-polls-per-hour limit is independent for A and B.
- Manual: Claude in a chat sends the owner 3 English questions, the polls arrive, after answering Telegram shows correctness and the explanation.

## Step 11. Collecting answers and results (`get_quiz_results`)

Do:
- The Telegram webhook (from step 9) subscribes to `allowed_updates: ["message", "callback_query", "poll_answer"]`. The webhook checks the `X-Telegram-Bot-Api-Secret-Token` header (secret from env `TG_WEBHOOK_SECRET`).
- Handling `poll_answer`: find the `poll_id` in `quizzes`, check that `user.id` matches `tg_id`, store `chosen_option` and `is_correct`. Unknown `poll_id` or a different user → ignore and log. A new answer (if the user retracted the vote) → overwrite; empty `option_ids` → delete the answer.
- MCP tool `get_quiz_results`:
  - input: `since: str | None` (ISO datetime, default: last 7 days), `topic: str | None`, `limit: int = 50`;
  - description for Claude: "Get the user's answers to previously sent quizzes: what they got wrong, what is unanswered, accuracy by topic. Call this before generating a new quiz to focus on weak spots.";
  - returns only the current user's data (by `tg_id` from the token): a summary (total, answered, correct, accuracy by topic) and a list of questions with `question`, `options`, `correct_option`, `chosen_option`, `is_correct`, `answered_at`, with unanswered ones listed separately.
- Retention: quizzes and answers older than 90 days are removed by the same cleanup from step 2.

Validation:
- Tests: `poll_answer` with the correct option → `is_correct = true`; with a wrong one → `false`; from a different `user.id` → not stored; without the secret header → 401; vote retracted → answer deleted.
- Isolation tests:
  - user A cannot see user B's quizzes and answers through `get_quiz_results`, including with a `topic` filter that only B has;
  - a `poll_answer` from B on a `poll_id` sent to A is not stored and does not change A's answer;
  - A's summary (accuracy, counts) is computed only from A's data.
- End-to-end isolation test: full OAuth flow for A and B, both get a quiz, both answer, each sees only their own data in `get_quiz_results`.
- Manual (end-to-end): a scheduled task sends a quiz in the morning, the owner answers some of the questions; the next run calls `get_quiz_results`, sees the mistakes and unanswered ones, sends a review and a new quiz focused on the mistakes.
- Add an example scheduled-task prompt to the README: "Call get_quiz_results for the last 2 days. Briefly review my mistakes via send_telegram. Then send 5 new B2-level English questions via send_quiz, focusing on the topics where I made mistakes."

## Step 12. Abuse protection, alerts, privacy

Context: the service is public (a pet project for LinkedIn), there is no allowlist, sign-in is open to any Telegram account. Hence limits, owner alerts and bans.

### 12.1 Limits

Do:
- Daily per-user limits: `DAILY_MESSAGE_LIMIT` for `send_telegram`, `DAILY_POLL_LIMIT` for `send_quiz` polls (day boundary by `TZ`). When exceeded - a clear error for Claude: "Daily limit reached, try again tomorrow".
- Per-IP rate limits: `/register` at most 10 per hour, `/authorize` and `/tg-callback` at most 30 per hour. Response 429. Take the IP from `X-Forwarded-For` only when it comes from Caddy (trusted proxy), otherwise from the connection.
- Every event is written to `usage_events`. The per-minute limit from step 6 and the per-hour limit from step 10 stay.
- Auto-cleanup: DCR clients that got no token within 24 hours are removed by the background cleanup. `usage_events` older than 30 days too.

Validation:
- Tests: the 201st message in a day → limit error, the next day (mocked time) works again; the 11th registration from one IP within an hour → 429, from another IP → passes; a client without tokens older than 24 hours is removed, a client with a token is not; `X-Forwarded-For` from an untrusted source is ignored.

### 12.2 Bans

Do:
- `tg-callback`: if `tg_id` is in `banned` - an "Access denied" page, no code is issued.
- On ban: all of the user's tokens are revoked immediately, the next MCP call gets 401.
- The ban is also checked in `load_access_token` (in case of a race).

Validation:
- Tests: a banned user cannot sign in; for a signed-in user, the next `send_telegram` call after the ban → 401; after `/unban` sign-in works again.

### 12.3 Owner alerts

Do:
- `app/alerts.py`: sends alerts to `ADMIN_TG_ID` through the same bot. Alerts do not count toward user limits. If sending an alert fails (e.g. the owner blocked the bot) - log only, the main request is not affected.
- Cooldown: the same `alert_key` at most once per hour (table `alerts_sent`).
- Instant alerts:
  - a user was rejected by a rate limit or daily limit 3+ times within an hour → `tg_id`, username, which limit, hints for the `/user <tg_id>` and `/ban <tg_id>` commands;
  - more than 20 new users within an hour;
  - more than 50 DCR registrations within an hour (across all IPs);
  - the Telegram Bot API returned 429 at the bot level (not for a specific user);
  - 5 or more 5xx responses within 5 minutes.
- Daily digest at `DIGEST_HOUR`: new users, active users in the last 24 hours, messages and polls in the last 24 hours, top 3 users by activity, number of banned users, who hit limits.
- Without `ADMIN_TG_ID`, alerts and the digest are off and the service still works.

Validation:
- Tests (respx): 3 limit rejections within an hour → exactly 1 alert to `ADMIN_TG_ID`; the 4th and 5th rejection in the same hour trigger no alert; after the cooldown the alert fires again; a failed alert send does not break the tool call; the digest has the right numbers on test data.
- Manual: with a lowered limit (e.g. `DAILY_MESSAGE_LIMIT=2`) trigger an alert and receive it in Telegram; check the digest with `uv run python -m app.cli digest`.

### 12.4 Admin bot commands

Do (accepted only from `ADMIN_TG_ID`, silently ignored from anyone else, like an unknown command):
- `/stats` - same as the digest, but for the current day.
- `/user <tg_id | @username>` - username, sign-up date, last sign-in, usage for the day and the week, banned or not.
- `/ban <tg_id> [reason]` - row in `banned`, tokens revoked, confirmation reply.
- `/unban <tg_id>`.

Validation:
- Tests: each command works from `ADMIN_TG_ID`; from another user - no reply and no effect (the ban is not applied).

### 12.5 `/forget` and privacy

Do:
- `/forget` command for any user: the bot asks for confirmation (inline button "Yes, delete"); after confirmation, the user's rows in `users`, `tokens`, `auth_codes`, `quizzes`, `quiz_answers`, `usage_events` are deleted. The row in `banned` (if any) stays. Reply: "Your data has been deleted. To use the bot again, reconnect the connector."
- `GET /privacy` - a placeholder page (plain HTML, no external resources) with this structure:
  - what is stored: Telegram id, username, first name, quizzes and answers, technical event logs (without message text);
  - what is not stored: the text of `send_telegram` messages;
  - retention: quizzes 90 days, events 30 days;
  - how to delete: the `/forget` command in the bot;
  - owner contact: `TODO`;
  - a note at the top: "Draft. Will be completed before the public launch."
- A link to `/privacy` on the `/login` page (below the widget) and in the bot's `/start` reply.
- A pre-publication checklist item in the README (before the LinkedIn post): fill in the privacy page (contact, legal basis), check `ADMIN_TG_ID`, check external monitoring.

Validation:
- Tests: after `/forget` there are no rows with this `tg_id` in the database except in `banned`; the old token → 401; `get_quiz_results` is empty after reconnecting.
- `curl https://notify.wentthrough.com/privacy` → 200, the page opens; the link is present on `/login` and in the `/start` reply.

### 12.6 External monitoring

Do:
- README instructions: Healthchecks.io or UptimeRobot (free plan) on `https://notify.wentthrough.com/health`, 5-minute interval, notifications to the owner's email or Telegram.
- `/health` checks database availability (a simple `SELECT 1`) and returns 503 if the database is unavailable.

Validation:
- Test: with the database unavailable, `/health` → 503.
- Manual: monitoring is set up; `docker compose stop app` → a down notification arrives within 10 minutes, and an up notification after `start`.

---

## Log

One entry per step, 2-5 lines: what was done, validation results, what is still open or deferred (e.g. manual checks that are only possible after deployment). New entries go at the bottom.

<!-- example:
### Step 1 - 2026-11-12
- Skeleton, Dockerfile, compose, /health.
- Validation: compose up ok, /health ok, missing BOT_TOKEN fails with an error, pytest 3/3.
- Open: nothing.
-->

### Step 1 - 2026-11-11
- Skeleton: uv project (Python 3.14), Starlette app with `GET /health`, env config with startup validation, Dockerfile (uv, non-root), compose, `.env.example`, CI (secrets + test jobs), PR template.
- Validation: compose up ok, `/health` → `{"status":"ok"}`, missing BOT_TOKEN exits with "missing required variables: BOT_TOKEN", both env_guard checks ok, `uv lock --check` ok, pytest 11/11.
- Note: locally `ENV_FILE=.env.example` must be set for every compose command (`exec`, `down` too), not only `up`.
- Open: Caddyfile is a stub, wired into compose in step 7.

### Step 2 - 2026-11-11
- `app/db.py` on SQLAlchemy Core (SQLite, WAL), portable schema for users, clients, pending_auth, auth_codes, tokens (BigInteger ids/timestamps, so Postgres is a URL change); access functions per table; `hash_secret()`, `new_secret()`. Codes and tokens are passed raw and stored only as SHA-256.
- Cleanup: background job in the app lifespan, runs at startup and then hourly; lookups also filter out expired rows between passes. Auth codes are consumed atomically (`UPDATE ... RETURNING`).
- Validation: pytest 38/38 (schema twice, Postgres DDL compiles, CRUD per table, 64-bit tg_id, expiry, cleanup, hash-only storage); compose up ok, DB created in `/data`, restart on an existing DB ok.
- Open: on a Linux host the bind-mounted `./data` must be writable by uid 10001 (Docker Desktop on macOS hides this) - handle in step 7.
