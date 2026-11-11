import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db import (
    AUTH_CODE_TTL,
    PENDING_AUTH_TTL,
    Database,
    hash_secret,
    metadata,
    new_secret,
    open_database,
    sqlite_url,
)

NOW = 1_800_000_000
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def dump_db(db: Database) -> str:
    """Every value stored in every table, as one string."""
    parts = []
    with db.engine.connect() as conn:
        for name in sa.inspect(conn).get_table_names():
            for row in conn.execute(sa.text(f"SELECT * FROM {name}")):
                parts.extend(str(v) for v in tuple(row))
    return "\n".join(parts)


def table_names(db: Database) -> set[str]:
    return set(sa.inspect(db.engine).get_table_names())


def column(db: Database, sql: str) -> list:
    with db.engine.connect() as conn:
        return list(conn.execute(sa.text(sql)).scalars())


def count(db: Database, table: str) -> int:
    return column(db, f"SELECT COUNT(*) FROM {table}")[0]


# helpers


def test_new_secret_is_random_and_long():
    a, b = new_secret(), new_secret()
    assert a != b
    assert len(a) >= 43


def test_hash_secret_is_sha256_hex():
    assert hash_secret("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# schema


def test_init_schema_is_idempotent(tmp_path):
    url = sqlite_url(str(tmp_path / "db.sqlite"))
    db = Database(url)
    db.init_schema()
    db.upsert_user(1, "alice", "Alice", now=NOW)
    db.init_schema()
    db.close()

    db = Database(url)
    db.init_schema()
    assert db.get_user(1) is not None
    db.close()


def test_schema_has_step_2_tables(db):
    assert {"users", "clients", "pending_auth", "auth_codes", "tokens"} <= table_names(db)


def test_schema_compiles_for_postgres():
    # Guards portability: the schema must stay expressible on Postgres.
    dialect = postgresql.dialect()
    for table in metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert "SERIAL" not in ddl.upper()
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_sqlite_pragmas(db):
    assert column(db, "PRAGMA journal_mode") == ["wal"]
    assert column(db, "PRAGMA foreign_keys") == [1]


def test_creates_parent_directory(tmp_path):
    db = Database(sqlite_url(str(tmp_path / "nested" / "dir" / "db.sqlite")))
    db.init_schema()
    db.close()
    assert (tmp_path / "nested" / "dir" / "db.sqlite").exists()


# users


def test_user_crud(db):
    created = db.upsert_user(42, "alice", "Alice", now=NOW)
    assert created.tg_id == 42
    assert created.created_at == created.last_login_at == NOW
    assert created.blocked_bot is False
    assert db.get_user(42) == created

    updated = db.upsert_user(42, "alice2", "Alicia", now=NOW + 100)
    assert updated.username == "alice2"
    assert updated.first_name == "Alicia"
    assert updated.created_at == NOW
    assert updated.last_login_at == NOW + 100

    assert db.set_blocked_bot(42, True) is True
    assert db.get_user(42).blocked_bot is True
    assert db.set_blocked_bot(42, False) is True
    assert db.get_user(42).blocked_bot is False
    assert db.set_blocked_bot(999, True) is False

    assert db.delete_user(42) is True
    assert db.get_user(42) is None
    assert db.delete_user(42) is False


def test_user_with_64_bit_telegram_id(db):
    big = 2**40
    assert db.upsert_user(big, "bob", "Bob", now=NOW).tg_id == big
    assert db.get_user(big).tg_id == big


def test_user_optional_fields(db):
    user = db.upsert_user(7, None, None, now=NOW)
    assert user.username is None
    assert user.first_name is None


# clients


def test_client_crud(db):
    uris = [REDIRECT, "http://localhost:6274/oauth/callback"]
    created = db.create_client("client-1", "Claude", uris, now=NOW)
    assert created.redirect_uris == tuple(uris)
    assert created.created_at == NOW
    assert db.get_client("client-1") == created
    assert db.get_client("unknown") is None

    with pytest.raises(sa.exc.IntegrityError):
        db.create_client("client-1", "Dup", uris, now=NOW)

    assert db.delete_client("client-1") is True
    assert db.get_client("client-1") is None
    assert db.delete_client("client-1") is False


# pending_auth


def test_pending_auth_crud(db):
    pending = db.create_pending_auth(
        "client-1", REDIRECT, CHALLENGE, "st4te", ["send"], "https://bridge.test/mcp", now=NOW
    )
    assert len(pending.id) >= 43
    assert pending.expires_at == NOW + PENDING_AUTH_TTL
    assert pending.scopes == ("send",)
    assert db.get_pending_auth(pending.id, now=NOW + 1) == pending

    assert db.delete_pending_auth(pending.id) is True
    assert db.get_pending_auth(pending.id, now=NOW + 1) is None
    assert db.delete_pending_auth(pending.id) is False


def test_pending_auth_ids_are_unique(db):
    a = db.create_pending_auth("c", REDIRECT, CHALLENGE, None, [], None, now=NOW)
    b = db.create_pending_auth("c", REDIRECT, CHALLENGE, None, [], None, now=NOW)
    assert a.id != b.id
    assert a.state is None and a.resource is None and a.scopes == ()


def test_pending_auth_expires(db):
    pending = db.create_pending_auth("c", REDIRECT, CHALLENGE, None, [], None, now=NOW)
    assert db.get_pending_auth(pending.id, now=NOW + PENDING_AUTH_TTL - 1) is not None
    assert db.get_pending_auth(pending.id, now=NOW + PENDING_AUTH_TTL) is None


# auth_codes


def create_code(db, code, now=NOW):
    return db.create_auth_code(
        code, 42, "client-1", REDIRECT, CHALLENGE, ["send"], "https://bridge.test/mcp", now=now
    )


def test_auth_code_crud(db):
    code = new_secret()
    created = create_code(db, code)
    assert created.tg_id == 42
    assert created.expires_at == NOW + AUTH_CODE_TTL
    assert created.used is False
    assert db.get_auth_code(code) == created
    assert db.get_auth_code("wrong") is None


def test_auth_code_consumed_once(db):
    code = new_secret()
    create_code(db, code)

    consumed = db.consume_auth_code(code, now=NOW + 1)
    assert consumed is not None
    assert consumed.used is True
    assert consumed.tg_id == 42

    assert db.consume_auth_code(code, now=NOW + 2) is None
    assert db.get_auth_code(code).used is True


def test_expired_auth_code_cannot_be_consumed(db):
    code = new_secret()
    create_code(db, code)
    assert db.consume_auth_code(code, now=NOW + AUTH_CODE_TTL) is None
    assert db.get_auth_code(code).used is False


def test_unknown_auth_code_cannot_be_consumed(db):
    assert db.consume_auth_code("nope", now=NOW) is None


def test_auth_code_stored_only_as_hash(db):
    code = new_secret()
    create_code(db, code)
    assert column(db, "SELECT code_hash FROM auth_codes") == [hash_secret(code)]
    assert code not in dump_db(db)


# tokens


def test_token_crud(db):
    access, refresh = new_secret(), new_secret()
    a = db.create_token(access, "access", 42, "client-1", ["send"], ttl=3600, now=NOW)
    r = db.create_token(refresh, "refresh", 42, "client-1", ["send"], ttl=86400, now=NOW)
    assert a.expires_at == NOW + 3600
    assert r.expires_at == NOW + 86400

    assert db.get_token(access, "access", now=NOW + 1) == a
    assert db.get_token(refresh, "refresh", now=NOW + 1) == r
    # A token is only accepted as the kind it was issued as.
    assert db.get_token(access, "refresh", now=NOW + 1) is None
    assert db.get_token(refresh, "access", now=NOW + 1) is None

    assert db.revoke_token(access) is True
    assert db.get_token(access, "access", now=NOW + 1) is None
    assert db.revoke_token(access) is False
    assert db.get_token(refresh, "refresh", now=NOW + 1) is not None


def test_token_expires(db):
    token = new_secret()
    db.create_token(token, "access", 42, "c", [], ttl=60, now=NOW)
    assert db.get_token(token, "access", now=NOW + 59) is not None
    assert db.get_token(token, "access", now=NOW + 60) is None


def test_token_rejects_unknown_kind(db):
    with pytest.raises(ValueError):
        db.create_token(new_secret(), "id", 42, "c", [], ttl=60, now=NOW)


def test_revoke_user_tokens(db):
    a1, a2, b1 = new_secret(), new_secret(), new_secret()
    other_client = new_secret()
    db.create_token(a1, "access", 1, "c1", [], ttl=60, now=NOW)
    db.create_token(a2, "refresh", 1, "c1", [], ttl=60, now=NOW)
    db.create_token(other_client, "access", 1, "c2", [], ttl=60, now=NOW)
    db.create_token(b1, "access", 2, "c1", [], ttl=60, now=NOW)

    assert db.revoke_user_tokens(1, client_id="c1") == 2
    assert db.get_token(other_client, "access", now=NOW) is not None
    assert db.revoke_user_tokens(1) == 1
    assert db.get_token(other_client, "access", now=NOW) is None
    # Another user's tokens are untouched.
    assert db.get_token(b1, "access", now=NOW) is not None


def test_tokens_stored_only_as_hash(db):
    access, refresh = new_secret(), new_secret()
    db.create_token(access, "access", 42, "c", [], ttl=60, now=NOW)
    db.create_token(refresh, "refresh", 42, "c", [], ttl=60, now=NOW)
    hashes = set(column(db, "SELECT token_hash FROM tokens"))
    assert hashes == {hash_secret(access), hash_secret(refresh)}
    dump = dump_db(db)
    assert access not in dump
    assert refresh not in dump


# cleanup


def test_cleanup_removes_only_expired_rows(db):
    old_pending = db.create_pending_auth("c", REDIRECT, CHALLENGE, None, [], None, now=NOW - 1000)
    new_pending = db.create_pending_auth("c", REDIRECT, CHALLENGE, None, [], None, now=NOW)
    old_code, new_code = new_secret(), new_secret()
    create_code(db, old_code, now=NOW - 1000)
    create_code(db, new_code, now=NOW)
    old_token, new_token = new_secret(), new_secret()
    db.create_token(old_token, "access", 42, "c", [], ttl=10, now=NOW - 1000)
    db.create_token(new_token, "refresh", 42, "c", [], ttl=1000, now=NOW)
    db.upsert_user(42, "alice", "Alice", now=NOW - 10_000)
    db.create_client("c", "Claude", [REDIRECT], now=NOW - 10_000)

    result = db.cleanup_expired(now=NOW + 1)

    assert (result.pending_auth, result.auth_codes, result.tokens) == (1, 1, 1)
    assert count(db, "pending_auth") == count(db, "auth_codes") == count(db, "tokens") == 1
    assert db.get_pending_auth(new_pending.id, now=NOW + 1) is not None
    assert db.get_pending_auth(old_pending.id, now=NOW - 999) is None
    assert db.get_auth_code(new_code) is not None
    assert db.get_auth_code(old_code) is None
    assert db.get_token(new_token, "refresh", now=NOW + 1) is not None
    # Users and clients never expire.
    assert db.get_user(42) is not None
    assert db.get_client("c") is not None

    assert db.cleanup_expired(now=NOW + 1).tokens == 0


def test_cleanup_job_runs_in_background(tmp_path):
    async def scenario():
        url = sqlite_url(str(tmp_path / "db.sqlite"))
        seed = Database(url)
        seed.init_schema()
        seed.create_token(new_secret(), "access", 1, "c", [], ttl=-1)
        seed.close()

        async with open_database(url, cleanup_interval=0.01) as db:
            # The first pass runs right away.
            for _ in range(100):
                await asyncio.sleep(0.01)
                if count(db, "tokens") == 0:
                    break
            else:
                pytest.fail("startup cleanup did not run")

            # Later passes keep running on the interval.
            db.create_token(new_secret(), "access", 1, "c", [], ttl=-1)
            for _ in range(100):
                await asyncio.sleep(0.01)
                if count(db, "tokens") == 0:
                    break
            else:
                pytest.fail("periodic cleanup did not run")

    asyncio.run(scenario())


def test_app_opens_database_on_startup(client, settings):
    assert client.app.state.db.get_user(1) is None
    db = Database(sqlite_url(settings.db_path))
    names = table_names(db)
    db.close()
    assert "users" in names
