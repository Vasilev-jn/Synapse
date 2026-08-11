from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.db import get_engine, init_db, make_session_factory


@pytest.fixture()
def db_session(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'test.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    session = session_factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()
