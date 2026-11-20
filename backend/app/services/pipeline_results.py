"""跨模块管道产物的统一读写。

管道产物统一落在 store 的 ``_pipeline_results`` 命名空间里，按 pipeline_id 幂等覆盖。
把读写收在一个模块里，避免管道编排器与对比销账服务互相 import 形成环。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

TABLE = "_pipeline_results"


def write_result(pipeline_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """写入或覆盖一条管道产物，保留首次创建时间。"""
    table = store.rows(TABLE)
    for row in table:
        if row.get("pipeline_id") != pipeline_id:
            continue
        row.update(payload)
        row["updated_at"] = datetime.now().isoformat()
        return row
    entry: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "created_at": datetime.now().isoformat(),
    }
    entry.update(payload)
    table.append(entry)
    return entry


def read_result(pipeline_id: str) -> dict[str, Any] | None:
    """读取一条管道产物；不存在时返回 None。"""
    for row in store.rows(TABLE):
        if row.get("pipeline_id") == pipeline_id:
            return dict(row)
    return None


def list_results(pipeline_id: str | None = None) -> list[dict[str, Any]]:
    """按 pipeline_id 列出管道产物；不传则返回全部。"""
    return [
        dict(row) for row in store.rows(TABLE)
        if pipeline_id is None or row.get("pipeline_id") == pipeline_id
    ]
