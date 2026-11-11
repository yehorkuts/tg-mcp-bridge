"""Database storage: schema, access functions and cleanup of expired rows.

Built on SQLAlchemy Core with portable types, so the backend is chosen by the
database URL. Production runs SQLite (a file on a volume); moving to Postgres
should only need a different URL and the driver dependency.

Raw authorization codes and tokens never reach the database: the access
functions take the raw value and store only its SHA-256 hash.

Timestamps are stored as integer Unix seconds. Every function that depends on
the current time takes an optional `now` so tests can control the clock.
"""

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

log = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 3600
PENDING_AUTH_TTL = 600
AUTH_CODE_TTL = 300

TokenKind = Literal["access", "refresh"]
TOKEN_KINDS: tuple[str, ...] = ("access", "refresh")

metadata = sa.MetaData()

# Telegram ids and Unix timestamps can exceed 32 bits, hence BigInteger.
users = sa.Table(
    "users",
    metadata,
    sa.Column("tg_id", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("username", sa.Text),
    sa.Column("first_name", sa.Text),
    sa.Column("created_at", sa.BigInteger, nullable=False),
    sa.Column("last_login_at", sa.BigInteger, nullable=False),
    sa.Column("blocked_bot", sa.Boolean, nullable=False, server_default=sa.false()),
)

clients = sa.Table(
    "clients",
    metadata,
    sa.Column("client_id", sa.Text, primary_key=True),
    sa.Column("client_name", sa.Text),
    sa.Column("redirect_uris", sa.JSON, nullable=False),
    sa.Column("created_at", sa.BigInteger, nullable=False),
)

pending_auth = sa.Table(
    "pending_auth",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("client_id", sa.Text, nullable=False),
    sa.Column("redirect_uri", sa.Text, nullable=False),
    sa.Column("code_challenge", sa.Text, nullable=False),
    sa.Column("state", sa.Text),
    sa.Column("scopes", sa.Text, nullable=False, server_default=""),
    sa.Column("resource", sa.Text),
    sa.Column("expires_at", sa.BigInteger, nullable=False, index=True),
)

auth_codes = sa.Table(
    "auth_codes",
    metadata,
    sa.Column("code_hash", sa.Text, primary_key=True),
    sa.Column("tg_id", sa.BigInteger, nullable=False),
    sa.Column("client_id", sa.Text, nullable=False),
    sa.Column("redirect_uri", sa.Text, nullable=False),
    sa.Column("code_challenge", sa.Text, nullable=False),
    sa.Column("scopes", sa.Text, nullable=False, server_default=""),
    sa.Column("resource", sa.Text),
    sa.Column("expires_at", sa.BigInteger, nullable=False, index=True),
    sa.Column("used", sa.Boolean, nullable=False, server_default=sa.false()),
)

tokens = sa.Table(
    "tokens",
    metadata,
    sa.Column("token_hash", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("tg_id", sa.BigInteger, nullable=False, index=True),
    sa.Column("client_id", sa.Text, nullable=False),
    sa.Column("scopes", sa.Text, nullable=False, server_default=""),
    sa.Column("expires_at", sa.BigInteger, nullable=False, index=True),
    sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_tokens_kind"),
)

# Dialect-specific INSERT constructs: both support ON CONFLICT and RETURNING
# with the same API, which keeps upserts portable.
_INSERTS = {"sqlite": sqlite.insert, "postgresql": postgresql.insert}


def new_secret() -> str:
    """A fresh random value for codes, tokens and opaque ids."""
    return secrets.token_urlsafe(32)


def hash_secret(raw: str) -> str:
    """SHA-256 hex digest: the only form in which codes and tokens are stored."""
    return hashlib.sha256(raw.encode()).hexdigest()


def sqlite_url(path: str) -> str:
    """SQLAlchemy URL for a SQLite file, creating its parent directory."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path}"


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _scopes_to_db(scopes: Iterable[str]) -> str:
    return " ".join(scopes)


def _scopes_from_db(raw: str) -> tuple[str, ...]:
    return tuple(raw.split())


def _sqlite_on_connect(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


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
    """Access functions over a SQLAlchemy engine.

    Every call runs in its own short transaction. Writes that must be atomic
    (e.g. consuming an auth code) are a single statement.
    """

    def __init__(self, url: str) -> None:
        self.engine = sa.create_engine(url)
        if self.engine.dialect.name == "sqlite":
            sa.event.listen(self.engine, "connect", _sqlite_on_connect)
        self._insert = _INSERTS[self.engine.dialect.name]

    def close(self) -> None:
        self.engine.dispose()

    def init_schema(self) -> None:
        """Create tables and indexes. Safe to call any number of times."""
        metadata.create_all(self.engine)

    def _one(self, stmt: sa.Executable) -> Mapping[str, Any] | None:
        with self.engine.begin() as conn:
            return conn.execute(stmt).mappings().first()

    def _rowcount(self, stmt: sa.Executable) -> int:
        with self.engine.begin() as conn:
            return conn.execute(stmt).rowcount

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
        stmt = self._insert(users).values(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            created_at=ts,
            last_login_at=ts,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[users.c.tg_id],
            set_={
                "username": stmt.excluded.username,
                "first_name": stmt.excluded.first_name,
                "last_login_at": stmt.excluded.last_login_at,
            },
        ).returning(*users.c)
        row = self._one(stmt)
        assert row is not None
        return _user(row)

    def get_user(self, tg_id: int) -> User | None:
        row = self._one(sa.select(users).where(users.c.tg_id == tg_id))
        return _user(row) if row else None

    def set_blocked_bot(self, tg_id: int, blocked: bool) -> bool:
        stmt = users.update().where(users.c.tg_id == tg_id).values(blocked_bot=blocked)
        return self._rowcount(stmt) > 0

    def delete_user(self, tg_id: int) -> bool:
        return self._rowcount(users.delete().where(users.c.tg_id == tg_id)) > 0

    # clients

    def create_client(
        self,
        client_id: str,
        client_name: str | None,
        redirect_uris: Iterable[str],
        now: int | None = None,
    ) -> Client:
        stmt = (
            clients.insert()
            .values(
                client_id=client_id,
                client_name=client_name,
                redirect_uris=list(redirect_uris),
                created_at=_now(now),
            )
            .returning(*clients.c)
        )
        row = self._one(stmt)
        assert row is not None
        return _client(row)

    def get_client(self, client_id: str) -> Client | None:
        row = self._one(sa.select(clients).where(clients.c.client_id == client_id))
        return _client(row) if row else None

    def delete_client(self, client_id: str) -> bool:
        return self._rowcount(clients.delete().where(clients.c.client_id == client_id)) > 0

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
        stmt = (
            pending_auth.insert()
            .values(
                id=new_secret(),
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                state=state,
                scopes=_scopes_to_db(scopes),
                resource=resource,
                expires_at=_now(now) + ttl,
            )
            .returning(*pending_auth.c)
        )
        row = self._one(stmt)
        assert row is not None
        return _pending_auth(row)

    def get_pending_auth(self, id: str, now: int | None = None) -> PendingAuth | None:
        """The pending request, or None if it is unknown or expired."""
        row = self._one(
            sa.select(pending_auth).where(
                pending_auth.c.id == id, pending_auth.c.expires_at > _now(now)
            )
        )
        return _pending_auth(row) if row else None

    def delete_pending_auth(self, id: str) -> bool:
        return self._rowcount(pending_auth.delete().where(pending_auth.c.id == id)) > 0

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
        stmt = (
            auth_codes.insert()
            .values(
                code_hash=hash_secret(code),
                tg_id=tg_id,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                scopes=_scopes_to_db(scopes),
                resource=resource,
                expires_at=_now(now) + ttl,
            )
            .returning(*auth_codes.c)
        )
        row = self._one(stmt)
        assert row is not None
        return _auth_code(row)

    def get_auth_code(self, code: str) -> AuthCode | None:
        """The code as stored, including used or expired ones (e.g. to detect reuse)."""
        row = self._one(sa.select(auth_codes).where(auth_codes.c.code_hash == hash_secret(code)))
        return _auth_code(row) if row else None

    def consume_auth_code(self, code: str, now: int | None = None) -> AuthCode | None:
        """Atomically mark the code used and return it.

        Returns None if the code is unknown, expired or already used, so a code
        can be exchanged at most once.
        """
        stmt = (
            auth_codes.update()
            .where(
                auth_codes.c.code_hash == hash_secret(code),
                auth_codes.c.used == sa.false(),
                auth_codes.c.expires_at > _now(now),
            )
            .values(used=True)
            .returning(*auth_codes.c)
        )
        row = self._one(stmt)
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
        stmt = (
            tokens.insert()
            .values(
                token_hash=hash_secret(token),
                kind=kind,
                tg_id=tg_id,
                client_id=client_id,
                scopes=_scopes_to_db(scopes),
                expires_at=_now(now) + ttl,
            )
            .returning(*tokens.c)
        )
        row = self._one(stmt)
        assert row is not None
        return _token(row)

    def get_token(self, token: str, kind: TokenKind, now: int | None = None) -> Token | None:
        """The token if it exists, has this kind, is not revoked and not expired."""
        row = self._one(
            sa.select(tokens).where(
                tokens.c.token_hash == hash_secret(token),
                tokens.c.kind == kind,
                tokens.c.revoked == sa.false(),
                tokens.c.expires_at > _now(now),
            )
        )
        return _token(row) if row else None

    def revoke_token(self, token: str) -> bool:
        stmt = (
            tokens.update()
            .where(tokens.c.token_hash == hash_secret(token), tokens.c.revoked == sa.false())
            .values(revoked=True)
        )
        return self._rowcount(stmt) > 0

    def revoke_user_tokens(self, tg_id: int, client_id: str | None = None) -> int:
        """Revoke all tokens of a user, optionally only those issued to one client."""
        stmt = tokens.update().where(tokens.c.tg_id == tg_id, tokens.c.revoked == sa.false())
        if client_id is not None:
            stmt = stmt.where(tokens.c.client_id == client_id)
        return self._rowcount(stmt.values(revoked=True))

    # cleanup

    def cleanup_expired(self, now: int | None = None) -> CleanupResult:
        """Delete expired pending requests, codes and tokens in one transaction."""
        ts = _now(now)
        with self.engine.begin() as conn:
            counts = [
                conn.execute(table.delete().where(table.c.expires_at <= ts)).rowcount
                for table in (pending_auth, auth_codes, tokens)
            ]
        return CleanupResult(*counts)


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
    url: str, cleanup_interval: float = CLEANUP_INTERVAL_SECONDS
) -> AsyncIterator[Database]:
    """Open the database, create the schema and run the hourly cleanup job."""
    db = Database(url)
    db.init_schema()
    task = asyncio.create_task(run_cleanup_loop(db, cleanup_interval))
    try:
        yield db
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        db.close()


def _user(row: Mapping[str, Any]) -> User:
    return User(
        tg_id=row["tg_id"],
        username=row["username"],
        first_name=row["first_name"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        blocked_bot=bool(row["blocked_bot"]),
    )


def _client(row: Mapping[str, Any]) -> Client:
    return Client(
        client_id=row["client_id"],
        client_name=row["client_name"],
        redirect_uris=tuple(row["redirect_uris"]),
        created_at=row["created_at"],
    )


def _pending_auth(row: Mapping[str, Any]) -> PendingAuth:
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


def _auth_code(row: Mapping[str, Any]) -> AuthCode:
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


def _token(row: Mapping[str, Any]) -> Token:
    return Token(
        kind=row["kind"],
        tg_id=row["tg_id"],
        client_id=row["client_id"],
        scopes=_scopes_from_db(row["scopes"]),
        expires_at=row["expires_at"],
        revoked=bool(row["revoked"]),
    )
