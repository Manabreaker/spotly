import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from spotly.config import Settings
from spotly.db import Base
from spotly.main import build_app


@pytest.fixture
def client(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL for isolated PostgreSQL integration tests")
    assert url.rsplit("/", 1)[-1].endswith("_test"), "never reset a non-test database"

    async def reset():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(reset())
    with TestClient(build_app(Settings(database_url=url, data_dir=tmp_path))) as c:
        yield c
