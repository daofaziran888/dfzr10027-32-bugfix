"""对比分析业务规则：状态流转、字段校验、筛选口径与差异复算都收在这里。

对比任务的下游是「可销账清单」：对比差异一旦发布，就按触点进入本班的
销账范围；补报重算只更新差异值，不重复生成销账条目。
"""
from __future__ import annotations

from typing import Any

from app.services.attribution_engine import compute_attribution_models
from app.store import store

MODULE = "compare_job"
LIST_FIELDS = ["任务编号", "基准数据", "对比数据", "比对字段", "差异阈值", "对比周期", "差异行数", "处理结论"]
REQUIRED_FIELDS = ["任务编号", "基准数据", "对比数据"]
STATUS_ORDER = ["待执行", "对比中", "已完成", "差异显著"]
ACTION_RULES = {"执行对比": "对比中", "标记完成": "已完成", "标记差异": "差异显著"}
NEGATIVE_ACTIONS = []

# 归因对比差异单：按触点发布、按分复算、按批次幂等。
DIFF_RESULTS = "compare_diff_rows"
RECON_STATE = "compare_reconciliation"
BATCH_POINTER = "_compare_batch_pointer"
DIFF_BATCHES = "_compare_diff_batches"


def _write_batch(batch_id: str, payload: dict[str, Any]) -> None:
    """批次发布信息单独存在对比模块里，不混进跨模块管道产物。"""
    table = store.rows(DIFF_BATCHES)
    for row in table:
        if row.get("batch_id") == batch_id:
            row.update(payload)
            return
    table.append({**payload, "batch_id": batch_id})


def _read_batch(batch_id: str) -> dict[str, Any] | None:
    for row in store.rows(DIFF_BATCHES):
        if row.get("batch_id") == batch_id:
            return dict(row)
    return None


def _write_pointer(link_code: str, batch_id: str) -> None:
    table = store.rows(BATCH_POINTER)
    for row in table:
        if row.get("link_code") == link_code:
            row["batch_id"] = batch_id
            return
    table.append({"link_code": link_code, "batch_id": batch_id})


def _read_pointer(link_code: str | None) -> str | None:
    for row in store.rows(BATCH_POINTER):
        if row.get("link_code") == (link_code or "*"):
            return row.get("batch_id")
    return None


def _fmt_precision(value: Any, precision: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), precision)


def _diff_row_key(link_code: str, touch_type: str) -> str:
    return f"{link_code}|{touch_type}"


def _amount_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "销账时首次归因": row.get("首次归因"),
        "销账时末次归因": row.get("末次归因"),
        "销账时差异": row.get("差异"),
    }


def _same_amounts(state: dict[str, Any], row: dict[str, Any]) -> bool:
    snapshot = _amount_snapshot(row)
    return all(state.get(key) == value for key, value in snapshot.items())


class CompareJobService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("任务编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"对比任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于对比分析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"对比任务已{action}"

    # ------------------------------------------------------------------
    # 差异清单：由归因基准数据与对照金额直接复算
    # ------------------------------------------------------------------

    def batches(self, link_code: str | None = None) -> list[dict[str, Any]]:
        """比对任务可消费的差异批次元数据（不含管道产物本体）。"""
        rows = store.rows(DIFF_BATCHES)
        if link_code is not None:
            rows = [row for row in rows if row.get("link_code") == link_code]
        return [
            {
                "batch_id": row.get("batch_id"),
                "link_code": row.get("link_code"),
                "precision": row.get("precision"),
                "coverage": row.get("coverage"),
            }
            for row in rows
        ]

    def latest_batch_id(self, link_code: str | None = None) -> str | None:
        """每个链路各自记住自己最新发布的批次，避免不同链路互相串批。"""
        return _read_pointer(link_code)

    def published_rows(
        self, batch_id: str | None = None, link_code: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取某一批已发布的差异行；不指定批次时给最新一批。"""
        if batch_id is None:
            batch_id = self.latest_batch_id(link_code)
        if batch_id is None:
            return []
        return [
            dict(row) for row in store.rows(DIFF_RESULTS)
            if row.get("批次号") == batch_id
            and (link_code is None or row.get("链路编号") == link_code)
        ]

    def publish_diff_rows(
        self,
        link_code: str,
        touches: list[dict[str, Any]],
        *,
        batch_id: str,
        precision: int = 2,
    ) -> dict[str, Any]:
        """计算并发布一批差异行。

        同一批次重复发布只覆盖同一批行（不追加重复行）；差异值仍由
        两侧已发布金额推出，供下游按分复算。
        """
        models = compute_attribution_models(link_code, touches, precision=precision)
        if "error" in models:
            return {"error": models["error"]}

        table = store.rows(DIFF_RESULTS)
        # 先把本批次已有行按触点建索引，重复发布时原地覆盖。
        existing = {
            _diff_row_key(row.get("链路编号", ""), row.get("触点类型", "")): row
            for row in table if row.get("批次号") == batch_id
        }
        precision = int(models.get("precision", 2))
        publish_rows: list[dict[str, Any]] = []
        for row in models["compare_rows"]:
            key = _diff_row_key(link_code, row["触点类型"])
            entry = existing.get(key)
            if entry is None:
                entry = {
                    "id": max((int(r.get("id", 0)) for r in table), default=0) + 1,
                    "批次号": batch_id,
                    "链路编号": link_code,
                    "触点类型": row["触点类型"],
                }
                table.append(entry)
            entry.update({
                "首次归因": _fmt_precision(row["首次归因"], precision),
                "末次归因": _fmt_precision(row["末次归因"], precision),
                "差异": _fmt_precision(row["差异"], precision),
                "精度位数": precision,
                "可比状态": row["可比状态"],
                "首次有数据": row["首次有数据"],
                "末次有数据": row["末次有数据"],
                "已确认差异": row["已确认差异"],
                "在比较范围内": row["在比较范围内"],
            })
            publish_rows.append(row)

        _write_batch(batch_id, {
            "link_code": link_code,
            "precision": precision,
            "rows": publish_rows,
            "coverage": models.get("coverage", {}),
        })
        _write_pointer(link_code, batch_id)
        _write_pointer("*", batch_id)
        return models

    # ------------------------------------------------------------------
    # 本班可销账清单
    # ------------------------------------------------------------------

    def claimable(self, link_code: str | None = None) -> dict[str, Any]:
        """算出本班可销账范围。

        规则：
          * 只有「双方可比」的差异行可以进入销账范围；单侧待补的触点
            先在清单里标出来，但不允许销账。
          * 已核平的触点一开始就能关掉（差异为 0）。
          * 同一批次重提只更新差异，不重复生成销账条目。
        """
        batch_id = self.latest_batch_id(link_code)
        rows = self.published_rows(batch_id, link_code)
        claimed = {
            row.get("触点类型"): row
            for row in store.rows(RECON_STATE)
            if row.get("批次号") == batch_id
            and (link_code is None or row.get("链路编号") == link_code)
        }

        items: list[dict[str, Any]] = []
        pending = 0
        for row in rows:
            status = row.get("可比状态")
            diff = row.get("差异")
            if status != "双方可比":
                pending += 1
                continue
            state = claimed.get(row["触点类型"], {})
            # 补数后如果这一侧的金额变了，旧销账结论就作废，必须重新核。
            stale = bool(state.get("已销账")) and not _same_amounts(state, row)
            items.append({
                "触点类型": row.get("触点类型"),
                "首次归因": row.get("首次归因"),
                "末次归因": row.get("末次归因"),
                "差异": diff,
                "已核平": diff == 0,
                "已销账": bool(state.get("已销账")) and not stale,
                "需复核": stale,
                "销账批次": None if stale else state.get("销账批次"),
            })

        unsettled = [item for item in items if not item["已销账"]]
        return {
            "batch_id": batch_id,
            "link_code": link_code,
            "precision": int(rows[0].get("精度位数", 2)) if rows else 2,
            "items": items,
            "可领条数": len(unsettled),
            "可销账条数": len(items),
            "已核平条数": sum(1 for item in items if item["已核平"]),
            "已销账条数": sum(1 for item in items if item["已销账"]),
            "待补报条数": pending,
            "empty_reason": (
                "尚未执行归因对比：该链路还没有已发布的差异批次"
                if batch_id is None else self._empty_reason(rows, unsettled)
            ),
            "已执行": batch_id is not None,
        }

    @staticmethod
    def _empty_reason(rows: list[dict[str, Any]], unsettled: list[dict[str, Any]]) -> str | None:
        if not rows:
            return "整批没有交易：归因链路没有可比较的成交触点"
        if all(row.get("可比状态") != "双方可比" for row in rows):
            return "一侧补报未到：所有触点都还只有单侧归因金额，暂不可销账"
        if not unsettled:
            if any(row.get("可比状态") != "双方可比" for row in rows):
                return "金额已核平：可比触点均已按分核对并销账，单侧触点仍缺另一侧补报"
            return "金额已核平：本批可比触点均已按分核对并完成销账"
        return None

    def settle(
        self,
        touch_types: list[str],
        *,
        settled_by: str,
        batch_id: str | None = None,
        link_code: str | None = None,
    ) -> dict[str, Any]:
        """按触点销账；同一批次同一触点重复提交不会重复销账。

        补数后金额发生变化时，旧销账记录不会被覆盖，而是把该触点判为
        「需重新核对」，必须重新提交一次销账。
        """
        if batch_id is None:
            batch_id = self.latest_batch_id(link_code)
        rows = self.published_rows(batch_id, link_code)
        index = {row["触点类型"]: row for row in rows}
        state_table = store.rows(RECON_STATE)

        settled: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        rejected: list[dict[str, str]] = []
        for touch in touch_types:
            row = index.get(touch)
            if row is None:
                rejected.append({"触点类型": touch, "原因": "不在本批差异清单内"})
                continue
            if row.get("可比状态") != "双方可比":
                rejected.append({"触点类型": touch, "原因": "一侧补报未到，暂不可销账"})
                continue
            existing = next(
                (r for r in state_table
                 if r.get("批次号") == batch_id
                 and r.get("触点类型") == touch
                 and r.get("链路编号") in (None, row.get("链路编号"))),
                None,
            )
            # 只有金额没变过才算重复销账；补数后金额变了必须重新销。
            if existing is not None and existing.get("已销账") and _same_amounts(existing, row):
                skipped.append({"触点类型": touch, "原因": "本批已销账，不重复销账"})
                continue
            if existing is None:
                existing = {"批次号": batch_id, "触点类型": touch}
                state_table.append(existing)
            existing.update({
                "已销账": True,
                "销账批次": batch_id,
                "链路编号": row.get("链路编号"),
                "销账人": settled_by,
                **_amount_snapshot(row),
            })
            settled.append({
                "触点类型": touch,
                "差异": row.get("差异"),
                "销账批次": batch_id,
            })

        precision = int(rows[0].get("精度位数", 2)) if rows else 2
        return {
            "batch_id": batch_id,
            "已销账": [item["触点类型"] for item in settled],
            "已处理触点": settled,
            "按分差额合计": _fmt_precision(sum(float(item["差异"] or 0) for item in settled), precision),
            "重复销账": skipped,
            "不可销账": rejected,
            "待处理触点": [r["触点类型"] for r in rows if r.get("可比状态") == "双方可比"],
        }

    # ------------------------------------------------------------------
    # 下游报表读取
    # ------------------------------------------------------------------

    def report_summary(
        self, batch_id: str | None = None, link_code: str | None = None,
    ) -> dict[str, Any]:
        """供下游报表读取：差异行与对账数据分开说明覆盖范围。"""
        if batch_id is None:
            batch_id = self.latest_batch_id()
        published = _read_batch(batch_id) if batch_id else None
        rows = self.published_rows(batch_id, link_code)
        claimed = {
            row.get("触点类型"): row
            for row in store.rows(RECON_STATE)
            if row.get("批次号") == batch_id
        }
        diff_rows = [row for row in rows if row.get("已确认差异")]
        coverage = (published or {}).get("coverage", {})
        precision = int((published or {}).get("precision", 2))
        settled_touches = [
            row for row in claimed.values()
            if row.get("已销账") and _same_amounts(row, next(
                (r for r in rows if r.get("触点类型") == row.get("触点类型")), {}))
        ]
        settled_names = {row.get("触点类型") for row in settled_touches}
        return {
            "batch_id": batch_id,
            "link_code": link_code,
            "精度位数": precision,
            "比较覆盖触点数": coverage.get("比较覆盖触点数", len(rows)),
            "已确认差异触点数": len(diff_rows),
            "已核平触点数": sum(1 for row in rows if row.get("可比状态") == "双方可比" and not row.get("已确认差异")),
            "单侧待补触点数": sum(1 for row in rows if row.get("可比状态") != "双方可比"),
            "差异清单": diff_rows,
            "已确认差异合计": _fmt_precision(sum(float(row.get("差异") or 0) for row in diff_rows), precision),
            "对账": {
                # 已确认差异的触点与已销账的触点在同一批发布行上对齐：
                # 销账金额与当前发布金额不一致的，回到未销账里等重新核对。
                "已销账触点": sorted(settled_names),
                "未销账触点": [
                    row.get("触点类型") for row in rows
                    if row.get("可比状态") == "双方可比" and row.get("触点类型") not in settled_names
                ],
                "需复核触点": [
                    row.get("触点类型") for row in claimed.values()
                    if row.get("已销账") and row.get("触点类型") not in settled_names
                ],
            },
        }
