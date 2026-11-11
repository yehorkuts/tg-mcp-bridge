# SPEC: tg-mcp-bridge

What we are building and the rules it follows. Implementation order and status live in PLAN.md, process rules in CLAUDE.md.

Repository: `tg-mcp-bridge`.

A remote MCP server that lets Claude (claude.ai, scheduled tasks, Claude Code) send messages to a user in Telegram. The user signs in with the Telegram Login Widget; the server itself implements OAuth 2.1 + DCR.

## Goals

- A user adds the service to claude.ai as a custom connector and signs in with Telegram.
- Claude gets a `send_telegram(text)` tool and sends messages to that user from a shared bot.
- Claude can send practice quizzes (for example, English) as native Telegram quiz polls: Telegram itself shows the user whether the answer was right, plus an explanation. The next Claude run fetches the results and adapts new questions to the mistakes.
- Works from regular chats and from scheduled tasks.

## Non-goals (MVP)

- Web dashboard, billing, multiple bots, Markdown formatting, attachments.
- Free-text incoming messages from users. The only incoming updates handled are `/start` (plus bot commands defined in PLAN.md) and quiz poll answers (`poll_answer`).
- Instant grading of free-text answers via the Claude API.

## Domain

- For now we use the subdomain `notify.wentthrough.com`. The domain `wentthrough.com` belongs to another project of the owner (registered at Namecheap); tg-mcp-bridge lives on it temporarily.
- The domain is never hardcoded: it always comes from `BASE_URL` / `DOMAIN`, so moving to a dedicated domain is an env and DNS change with no code changes.
- When moving: a new A record, a new `/setdomain` in BotFather, a new `BASE_URL`; users will have to reconnect the connector in claude.ai (the resource URL changes).

## Stack

- Python 3.12, the official `mcp` SDK, Starlette (or FastAPI), `httpx`
- SQLite (file on a volume), no ORM or minimal SQLAlchemy Core
- pytest + respx (HTTP mocking)
- Docker + docker compose, Caddy as a reverse proxy with automatic HTTPS

## Configuration (env)

| Variable | Example | Description |
|---|---|---|
| `BASE_URL` | `https://notify.wentthrough.com` | Public URL, no trailing slash |
| `DOMAIN` | `notify.wentthrough.com` | Domain for Caddy |
| `BOT_TOKEN` | `123:ABC` | Bot token from BotFather |
| `BOT_USERNAME` | `MyNotifyBot` | Bot username without @ |
| `TG_WEBHOOK_SECRET` | random string | Secret for the `X-Telegram-Bot-Api-Secret-Token` header (steps 9, 11) |
| `ADMIN_TG_ID` | `123456789` | Owner's Telegram id: alerts, digest, admin commands (step 12) |
| `DAILY_MESSAGE_LIMIT` | `200` | `send_telegram` messages per user per day (step 12) |
| `DAILY_POLL_LIMIT` | `100` | `send_quiz` polls per user per day (step 12) |
| `DIGEST_HOUR` | `9` | Hour the daily digest is sent (step 12) |
| `TZ` | `Europe/Warsaw` | Time zone for the digest and daily limits |
| `DB_PATH` | `/data/db.sqlite` | Database path |
| `ALLOWED_REDIRECT_URIS` | `https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback,http://localhost:6274/oauth/callback` | Allowed DCR redirect_uris (comma-separated). Localhost is for MCP Inspector |
| `ACCESS_TOKEN_TTL` | `3600` | Seconds |
| `REFRESH_TOKEN_TTL` | `2592000` | Seconds (30 days) |
| `LOG_LEVEL` | `INFO` | |

### Secrets

- The real `.env` exists only on the production server. The operator creates it by hand from `.env.example`. It never exists in the development environment where Claude Code works, and never in CI.
- `.env.example` contains placeholders only: secret keys (`BOT_TOKEN`, `TG_WEBHOOK_SECRET`, `ADMIN_TG_ID`) are empty or `<placeholder>`; non-secret keys may have harmless example values.
- `docker-compose.yml` reads env via `env_file: ${ENV_FILE:-.env}`. Local runs and CI use `ENV_FILE=.env.example`; the server uses the default `.env`.
- Tests never read env files: config is set in tests via fixtures/monkeypatch with fake values.
- Manual checks that need real secrets (a real bot token, sending to a real chat) are run by the operator, not by Claude Code.
- Enforcement, from softest to hardest:
  1. Rules in CLAUDE.md.
  2. `.claude/settings.json` permission deny rules for `.env` / `.env.*` (except `.env.example`) and for `.claude/**`.
  3. PreToolUse hook `.claude/hooks/env_guard.py`: blocks any tool call that touches `.env` / `.env.*`, including shell commands, and any write of real-looking secrets into `.env.example`. Hooks apply in every permission mode.
  4. Git side: `.gitignore` excludes `.env*` except `.env.example`; CI runs `env_guard.py --check .env.example`, `env_guard.py --check-repo` and gitleaks; GitHub push protection is on.
  5. The real `.env` is simply not present where Claude Code runs.
- `.claude/settings.json` and `.claude/hooks/**` are set up by the operator before step 1 and are never changed by Claude Code.

## Data model

- `users`: `tg_id` (PK, also the chat_id), `username`, `first_name`, `created_at`, `last_login_at`, `blocked_bot` (bool)
- `clients`: `client_id` (PK), `client_name`, `redirect_uris` (JSON), `created_at`
- `pending_auth`: `id` (PK, random), `client_id`, `redirect_uri`, `code_challenge`, `state`, `scopes`, `resource`, `expires_at` (10 min)
- `auth_codes`: `code_hash` (PK), `tg_id`, `client_id`, `redirect_uri`, `code_challenge`, `scopes`, `resource`, `expires_at` (5 min), `used` (bool)
- `tokens`: `token_hash` (PK), `kind` (`access` | `refresh`), `tg_id`, `client_id`, `scopes`, `expires_at`, `revoked` (bool)
- `quizzes`: `poll_id` (PK, poll id from Telegram), `tg_id`, `quiz_set_id` (one send = one set), `topic`, `question`, `options` (JSON), `correct_option` (int), `explanation`, `message_id`, `created_at`
- `quiz_answers`: `poll_id` (PK, FK → quizzes), `tg_id`, `chosen_option` (int), `is_correct` (bool), `answered_at`
- `banned`: `tg_id` (PK), `reason`, `banned_at` - kept separate from `users` so a ban survives `/forget`
- `usage_events`: `id`, `tg_id`, `kind` (`message` | `poll` | `rate_limited` | `login` | `register`), `ip` (only for `register`/`login`), `created_at` - for limits, alerts and the digest, kept for 30 days
- `alerts_sent`: `alert_key` (PK, e.g. `rate_limited:<tg_id>`), `sent_at` - for alert cooldown

Tables `quizzes` and `quiz_answers` are created in step 10; `banned`, `usage_events`, `alerts_sent` in step 12; the rest in step 2.

Codes and tokens are stored only as SHA-256 hashes. Raw values come from `secrets.token_urlsafe(32)`.

## Invariants (apply to every step)

- The message recipient is always determined by the `tg_id` from the access token. MCP tools have no `chat_id`, `tg_id`, `user_id` or similar parameters.
- Every data query is filtered by the current user's `tg_id`. User A can never read, change or send anything to user B.
- Codes and tokens are stored only as SHA-256 hashes.
- `send_telegram` message text is neither stored nor logged.
- Domain and URLs are never hardcoded, only taken from `BASE_URL` / `DOMAIN`.
- Secrets only from env; the repo contains only `.env.example` with placeholders. Claude Code never reads, creates or edits `.env` or `.env.*` other than `.env.example` (see "Secrets").
- External HTTP calls are mocked in tests (respx); tests never hit the real Telegram API.
- Everything in the repository is in English: code, comments, docs, commit messages, PR titles and descriptions, and user-facing texts (bot messages, HTML pages, tool descriptions).
