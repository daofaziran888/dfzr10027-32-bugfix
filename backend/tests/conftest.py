import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import SEED_ROWS  # noqa: E402
from app.store import store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_store():
    """每个用例就地重置同一份 store，避免模块级引用指向旧对象。"""
    store._tables.clear()
    store._tables.update({
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    })
    store._tables.setdefault("_pipeline_results", [])
    yield store
