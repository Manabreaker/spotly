from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def database(url):
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000 -c idle_in_transaction_session_timeout=15000",
        },
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)
