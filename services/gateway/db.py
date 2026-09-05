"""
Gateway persistence layer.

Deliberately built on SQLAlchemy Core (not the ORM) with only portable
column types (String/Numeric/DateTime/Boolean -- nothing Postgres-
specific like JSONB), so that switching the backing RDBMS is a matter of
changing DATABASE_URL and installing the matching async driver, not
rewriting this module. See README.md "Swapping the database" for the
exact steps to move this to MySQL.
"""
import os

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
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
)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine


async def init_db():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


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
