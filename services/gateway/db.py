"""
Gateway persistence layer.

Deliberately built on SQLAlchemy Core (not the ORM) with only portable
column types (String/Numeric/DateTime/Boolean -- nothing Postgres-
specific like JSONB), so that switching the backing RDBMS is a matter of
changing DATABASE_URL and installing the matching async driver, not
rewriting this module. See README.md "Swapping the database" for the
exact steps to move this to MySQL.
"""
import logging
import os

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    select,
)
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://gateway:gateway@postgres:5432/payments")

metadata = MetaData()

authorizations = Table(
    "authorizations",
    metadata,
    Column("transaction_id", String(36), primary_key=True),
    Column("merchant_id", String(20), nullable=False),
    Column("terminal_id", String(20), nullable=False),
    Column("pan", String(19), nullable=False),
    Column("expiry_date", String(4), nullable=False),  # ISO 8583 field 14 format: YYMM
    Column("card_network", String(20), nullable=False),
    Column("issuer_id", String(30), nullable=False),
    Column("transaction_type", String(20), nullable=False),
    Column("amount", Numeric(12, 2), nullable=False),
    Column("currency_code_numeric", String(3), nullable=False),
    Column("currency_code_alpha", String(3), nullable=False),
    Column("mcc", String(4)),
    Column("pos_entry_mode", String(15)),
    Column("pin_present", Boolean, nullable=False, server_default="false"),
    Column("stan", String(6)),
    Column("retrieval_reference_number", String(12)),
    Column("acquirer_id", String(30)),
    Column("status", String(20), nullable=False, server_default="PENDING"),
    Column("response_code", String(2)),
    Column("response_status", String(10)),
    Column("auth_code", String(6)),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("forwarded_at", DateTime(timezone=True)),
    Column("response_received_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("duration_ms", Integer),  # end-to-end: received_at -> completed_at
)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        # Pausing the issuer deliberately builds up a large backlog of
        # outstanding authorizations (see services/dashboard/app.py); when
        # it's resumed, all of them complete in a short burst. A small
        # default pool (5) queues or drops connections under that burst,
        # so both the pool and the overflow allowance are sized well past
        # normal steady-state load to absorb it.
        #
        # This pool is sized per gateway *replica* -- a single replica can
        # never have more than CONCURRENCY_LIMIT transactions in flight
        # (services/gateway/app.py's semaphore), each holding at most one
        # connection at a time, so pool_size + max_overflow here tracks
        # that same default (150) plus a little headroom rather than an
        # arbitrary bigger number. With N gateway replicas (podman-
        # compose.yml's gateway-1/gateway-2), total demand across all of
        # them is at most N x 150 -- Postgres's own max_connections there
        # is set to match (300, for the default 2 replicas). Scaling out
        # to more replicas means raising max_connections proportionally,
        # not this pool.
        _engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_size=50, max_overflow=100)
    return _engine


async def init_db():
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
    except Exception as exc:
        # Multiple gateway replicas can start concurrently (see
        # podman-compose.yml's gateway-1/gateway-2) and race to create the
        # schema on first boot; metadata.create_all()'s own check-then-
        # create isn't atomic across processes, so the loser of that race
        # can see a duplicate-table/index error here even though the
        # outcome (schema now exists) is exactly what we wanted. Logging
        # and continuing is safe -- any *other* kind of DB problem (bad
        # DATABASE_URL, Postgres unreachable) surfaces immediately anyway,
        # loudly, on this replica's very first insert_request() below.
        logging.getLogger(__name__).warning("init_db: %s (likely a concurrent replica race, continuing)", exc)


async def insert_request(record: dict):
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(authorizations.insert().values(**record))


async def update_response(transaction_id: str, fields: dict):
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            authorizations.update()
            .where(authorizations.c.transaction_id == transaction_id)
            .values(**fields)
        )


async def count_rows() -> int:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(select(authorizations.c.transaction_id))
        return len(result.fetchall())
