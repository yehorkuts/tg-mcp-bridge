"""SQLite storage: schema, access functions and cleanup of expired rows.

Raw authorization codes and tokens never reach the database: the access
functions take the raw value and store only its SHA-256 hash.

Timestamps are stored as integer Unix seconds. Every function that depends on
the current time takes an optional `now` so tests can control the clock.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 3600
PENDING_AUTH_TTL = 600
AUTH_CODE_TTL = 300

TokenKind = Literal["access", "refresh"]
TOKEN_KINDS: tuple[str, ...] = ("access", "refresh")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id         INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER NOT NULL,
    blocked_bot   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clients (
    client_id     TEXT PRIMARY KEY,
    client_name   TEXT,
    redirect_uris TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_auth (
    id             TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    state          TEXT,
    scopes         TEXT NOT NULL DEFAULT '',
    resource       TEXT,
    expires_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash      TEXT PRIMARY KEY,
    tg_id          INTEGER NOT NULL,
    client_id      TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes         TEXT NOT NULL DEFAULT '',
    resource       TEXT,
    expires_at     INTEGER NOT NULL,
    used           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    tg_id      INTEGER NOT NULL,
    client_id  TEXT NOT NULL,
    scopes     TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pending_auth_expires ON pending_auth (expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_codes_expires ON auth_codes (expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON tokens (expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_tg_id ON tokens (tg_id);
"""


def new_secret() -> str:
    """A fresh random value for codes, tokens and opaque ids."""
    return secrets.token_urlsafe(32)


def hash_secret(raw: str) -> str:
    """SHA-256 hex digest: the only form in which codes and tokens are stored."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _scopes_to_db(scopes: Iterable[str]) -> str:
    return " ".join(scopes)


def _scopes_from_db(raw: str) -> tuple[str, ...]:
    return tuple(raw.split())


@dataclass(frozen=True)
class User:
    tg_id: int
    username: str | None
    first_name: str | None
    created_at: int
    last_login_at: int
    blocked_bot: bool


@dataclass(frozen=True)
class Client:
    client_id: str
    client_name: str | None
    redirect_uris: tuple[str, ...]
    created_at: int


@dataclass(frozen=True)
class PendingAuth:
    id: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    state: str | None
    scopes: tuple[str, ...]
    resource: str | None
    expires_at: int


@dataclass(frozen=True)
class AuthCode:
    tg_id: int
    client_id: str
    redirect_uri: str
    code_challenge: str
    scopes: tuple[str, ...]
    resource: str | None
    expires_at: int
    used: bool


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    tg_id: int
    client_id: str
    scopes: tuple[str, ...]
    expires_at: int
    revoked: bool


@dataclass(frozen=True)
class CleanupResult:
    pending_auth: int
    auth_codes: int
    tokens: int


class Database:
    """A single SQLite connection shared by the app.

    The connection runs in autocommit mode; every write is one statement, so
    each one is atomic on its own. A lock serializes access in case a call
    ever comes from a worker thread.
    """

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        """Create tables and indexes. Safe to call any number of times."""
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _execute(self, sql: str, params: Iterable[object] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable[object] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    # users

    def upsert_user(
        self,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        now: int | None = None,
    ) -> User:
        """Create the user on first login, refresh profile and login time after."""
        ts = _now(now)
        row = self._one(
            """
            INSERT INTO users (tg_id, username, first_name, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (tg_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_login_at = excluded.last_login_at
            RETURNING *
            """,
            (tg_id, username, first_name, ts, ts),
        )
        assert row is not None
        return _user(row)

    def get_user(self, tg_id: int) -> User | None:
        row = self._one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return _user(row) if row else None

    def set_blocked_bot(self, tg_id: int, blocked: bool) -> bool:
        cur = self._execute(
            "UPDATE users SET blocked_bot = ? WHERE tg_id = ?", (int(blocked), tg_id)
        )
        return cur.rowcount > 0

    def delete_user(self, tg_id: int) -> bool:
        cur = self._execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        return cur.rowcount > 0

    # clients

    def create_client(
        self,
        client_id: str,
        client_name: str | None,
        redirect_uris: Iterable[str],
        now: int | None = None,
    ) -> Client:
        row = self._one(
            """
            INSERT INTO clients (client_id, client_name, redirect_uris, created_at)
            VALUES (?, ?, ?, ?)
            RETURNING *
            """,
            (client_id, client_name, json.dumps(list(redirect_uris)), _now(now)),
        )
        assert row is not None
        return _client(row)

    def get_client(self, client_id: str) -> Client | None:
        row = self._one("SELECT * FROM clients WHERE client_id = ?", (client_id,))
        return _client(row) if row else None

    def delete_client(self, client_id: str) -> bool:
        cur = self._execute("DELETE FROM clients WHERE client_id = ?", (client_id,))
        return cur.rowcount > 0

    # pending_auth

    def create_pending_auth(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str | None,
        scopes: Iterable[str],
        resource: str | None,
        now: int | None = None,
        ttl: int = PENDING_AUTH_TTL,
    ) -> PendingAuth:
        """Store an authorization request while the user signs in with Telegram."""
        row = self._one(
            """
            INSERT INTO pending_auth
                (id, client_id, redirect_uri, code_challenge, state, scopes, resource, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (
                new_secret(),
                client_id,
                redirect_uri,
                code_challenge,
                state,
                _scopes_to_db(scopes),
                resource,
                _now(now) + ttl,
            ),
        )
        assert row is not None
        return _pending_auth(row)

    def get_pending_auth(self, id: str, now: int | None = None) -> PendingAuth | None:
        """The pending request, or None if it is unknown or expired."""
        row = self._one(
            "SELECT * FROM pending_auth WHERE id = ? AND expires_at > ?", (id, _now(now))
        )
        return _pending_auth(row) if row else None

    def delete_pending_auth(self, id: str) -> bool:
        cur = self._execute("DELETE FROM pending_auth WHERE id = ?", (id,))
        return cur.rowcount > 0

    # auth_codes

    def create_auth_code(
        self,
        code: str,
        tg_id: int,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scopes: Iterable[str],
        resource: str | None,
        now: int | None = None,
        ttl: int = AUTH_CODE_TTL,
    ) -> AuthCode:
        """Store an authorization code. Only the hash of `code` is written."""
        row = self._one(
            """
            INSERT INTO auth_codes
                (code_hash, tg_id, client_id, redirect_uri, code_challenge, scopes, resource, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (
                hash_secret(code),
                tg_id,
                client_id,
                redirect_uri,
                code_challenge,
                _scopes_to_db(scopes),
                resource,
                _now(now) + ttl,
            ),
        )
        assert row is not None
        return _auth_code(row)

    def get_auth_code(self, code: str) -> AuthCode | None:
        """The code as stored, including used or expired ones (e.g. to detect reuse)."""
        row = self._one("SELECT * FROM auth_codes WHERE code_hash = ?", (hash_secret(code),))
        return _auth_code(row) if row else None

    def consume_auth_code(self, code: str, now: int | None = None) -> AuthCode | None:
        """Atomically mark the code used and return it.

        Returns None if the code is unknown, expired or already used, so a code
        can be exchanged at most once.
        """
        row = self._one(
            """
            UPDATE auth_codes SET used = 1
            WHERE code_hash = ? AND used = 0 AND expires_at > ?
            RETURNING *
            """,
            (hash_secret(code), _now(now)),
        )
        return _auth_code(row) if row else None

    # tokens

    def create_token(
        self,
        token: str,
        kind: TokenKind,
        tg_id: int,
        client_id: str,
        scopes: Iterable[str],
        ttl: int,
        now: int | None = None,
    ) -> Token:
        """Store an access or refresh token. Only the hash of `token` is written."""
        if kind not in TOKEN_KINDS:
            raise ValueError(f"unknown token kind: {kind!r}")
        row = self._one(
            """
            INSERT INTO tokens (token_hash, kind, tg_id, client_id, scopes, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING *
            """,
            (hash_secret(token), kind, tg_id, client_id, _scopes_to_db(scopes), _now(now) + ttl),
        )
        assert row is not None
        return _token(row)

    def get_token(self, token: str, kind: TokenKind, now: int | None = None) -> Token | None:
        """The token if it exists, has this kind, is not revoked and not expired."""
        row = self._one(
            """
            SELECT * FROM tokens
            WHERE token_hash = ? AND kind = ? AND revoked = 0 AND expires_at > ?
            """,
            (hash_secret(token), kind, _now(now)),
        )
        return _token(row) if row else None

    def revoke_token(self, token: str) -> bool:
        cur = self._execute(
            "UPDATE tokens SET revoked = 1 WHERE token_hash = ? AND revoked = 0",
            (hash_secret(token),),
        )
        return cur.rowcount > 0

    def revoke_user_tokens(self, tg_id: int, client_id: str | None = None) -> int:
        """Revoke all tokens of a user, optionally only those issued to one client."""
        if client_id is None:
            cur = self._execute(
                "UPDATE tokens SET revoked = 1 WHERE tg_id = ? AND revoked = 0", (tg_id,)
            )
        else:
            cur = self._execute(
                "UPDATE tokens SET revoked = 1 WHERE tg_id = ? AND client_id = ? AND revoked = 0",
                (tg_id, client_id),
            )
        return cur.rowcount

    # cleanup

    def cleanup_expired(self, now: int | None = None) -> CleanupResult:
        """Delete expired pending requests, codes and tokens."""
        ts = _now(now)
        with self._lock:
            pending = self._conn.execute(
                "DELETE FROM pending_auth WHERE expires_at <= ?", (ts,)
            ).rowcount
            codes = self._conn.execute(
                "DELETE FROM auth_codes WHERE expires_at <= ?", (ts,)
            ).rowcount
            tokens = self._conn.execute("DELETE FROM tokens WHERE expires_at <= ?", (ts,)).rowcount
        return CleanupResult(pending_auth=pending, auth_codes=codes, tokens=tokens)


async def run_cleanup_loop(db: Database, interval: float = CLEANUP_INTERVAL_SECONDS) -> None:
    """Run cleanup now and then every `interval` seconds until cancelled."""
    while True:
        try:
            result = db.cleanup_expired()
            log.info(
                "cleanup removed pending_auth=%d auth_codes=%d tokens=%d",
                result.pending_auth,
                result.auth_codes,
                result.tokens,
            )
        except Exception:
            log.exception("cleanup failed")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def open_database(
    path: str, cleanup_interval: float = CLEANUP_INTERVAL_SECONDS
) -> AsyncIterator[Database]:
    """Open the database, create the schema and run the hourly cleanup job."""
    db = Database(path)
    db.init_schema()
    task = asyncio.create_task(run_cleanup_loop(db, cleanup_interval))
    try:
        yield db
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        db.close()


def _user(row: sqlite3.Row) -> User:
    return User(
        tg_id=row["tg_id"],
        username=row["username"],
        first_name=row["first_name"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        blocked_bot=bool(row["blocked_bot"]),
    )


def _client(row: sqlite3.Row) -> Client:
    return Client(
        client_id=row["client_id"],
        client_name=row["client_name"],
        redirect_uris=tuple(json.loads(row["redirect_uris"])),
        created_at=row["created_at"],
    )


def _pending_auth(row: sqlite3.Row) -> PendingAuth:
    return PendingAuth(
        id=row["id"],
        client_id=row["client_id"],
        redirect_uri=row["redirect_uri"],
        code_challenge=row["code_challenge"],
        state=row["state"],
        scopes=_scopes_from_db(row["scopes"]),
        resource=row["resource"],
        expires_at=row["expires_at"],
    )


def _auth_code(row: sqlite3.Row) -> AuthCode:
    return AuthCode(
        tg_id=row["tg_id"],
        client_id=row["client_id"],
        redirect_uri=row["redirect_uri"],
        code_challenge=row["code_challenge"],
        scopes=_scopes_from_db(row["scopes"]),
        resource=row["resource"],
        expires_at=row["expires_at"],
        used=bool(row["used"]),
    )


def _token(row: sqlite3.Row) -> Token:
    return Token(
        kind=row["kind"],
        tg_id=row["tg_id"],
        client_id=row["client_id"],
        scopes=_scopes_from_db(row["scopes"]),
        expires_at=row["expires_at"],
        revoked=bool(row["revoked"]),
    )
