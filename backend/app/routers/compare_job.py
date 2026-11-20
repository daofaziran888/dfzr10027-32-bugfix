"""对比分析接口：维护对比任务，覆盖差异清单、本班销账与下游报表读取。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.compare_job import CompareJobService

router = APIRouter(prefix="/api/compare_job", tags=["对比分析"])

service = CompareJobService()

LIST_FIELDS = ["任务编号", "基准数据", "对比数据", "比对字段", "差异阈值", "对比周期", "差异行数", "处理结论"]
STATUSES = ["待执行", "对比中", "已完成", "差异显著"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="待执行、对比中、已完成、差异显著"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、任务编号关键字与状态过滤对比分析列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="compare_job.csv"'})


@router.get("/diff-rows")
def get_diff_rows(
    batch_id: str | None = Query(default=None, description="批次号，默认最新一批"),
    link_code: str | None = Query(default=None, description="链路编号"),
) -> dict:
    """按触点发布差异清单：两侧金额与差异同一精度，差异可用两侧金额复算。"""
    resolved = batch_id or service.latest_batch_id(link_code)
    rows = service.published_rows(resolved, link_code)
    precision = 2
    for row in rows:
        precision = int(row.get("精度位数", precision))
        break
    comparable = [row for row in rows if row.get("可比状态") == "双方可比"]
    return {
        "batch_id": resolved,
        "link_code": link_code,
        "precision": precision,
        "items": rows,
        "total": len(rows),
        "coverage": {
            "比较覆盖触点数": len(rows),
            "已确认差异触点数": sum(1 for row in rows if row.get("已确认差异")),
            "已核平触点数": sum(
                1 for row in rows
                if row.get("可比状态") == "双方可比" and not row.get("已确认差异")),
            "单侧待补触点数": sum(1 for row in rows if row.get("可比状态") != "双方可比"),
        },
        "empty_reason": (
            "尚未执行归因对比：该链路还没有已发布的差异批次"
            if resolved is None else service._empty_reason(rows, comparable)
        ),
    }


@router.get("/diff-batches")
def get_diff_batches(link_code: str | None = Query(default=None)) -> dict:
    """列出已发布的差异批次及其覆盖口径，便于下游按批次复算。"""
    items = service.batches(link_code)
    return {"items": items, "total": len(items)}


@router.get("/claimable")
def get_claimable(link_code: str | None = Query(default=None)) -> dict:
    """本班可销账清单：只包含双方可比、尚未销账的触点。"""
    return service.claimable(link_code)


@router.post("/settle", response_model=ActionResult)
def settle(payload: dict) -> ActionResult:
    """按触点销账；同一批次同一触点重复提交不会重复销账。"""
    touch_types = payload.get("触点类型") or payload.get("touches") or []
    if not isinstance(touch_types, list) or not touch_types:
        return ActionResult(ok=False, message="请至少选择一个触点进行销账")
    settled_by = str(payload.get("销账人") or payload.get("settled_by") or "值班管理员")
    result = service.settle(
        [str(item) for item in touch_types],
        settled_by=settled_by,
        batch_id=payload.get("batch_id"),
        link_code=payload.get("link_code"),
    )
    if result["已销账"]:
        message = f"已销账 {len(result['已销账'])} 个触点"
    elif result["重复销账"]:
        message = "本批触点已销账，未重复销账"
    else:
        message = "所选触点暂不可销账"
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/report")
def get_report(
    batch_id: str | None = Query(default=None),
    link_code: str | None = Query(default=None),
) -> dict:
    """下游报表读取：分别说明比较覆盖的触点与已确认差异的触点。"""
    return service.report_summary(batch_id, link_code)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条对比任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"对比任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条对比任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="对比任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条对比任务执行执行对比、标记完成、标记差异；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
