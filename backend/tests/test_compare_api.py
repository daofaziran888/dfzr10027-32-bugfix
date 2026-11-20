"""对比分析接口：差异清单、可销账清单与下游报表。"""
from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.requests import Request

from app.routers import compare_job as compare_router
from app.routers import pipeline as pipeline_router
from app.services.pipeline_orchestrator import run_attribution_compare_pipeline


def _asdict(result: Any) -> dict[str, Any]:
    return json.loads(result.model_dump_json())


def test_full_shift_flow():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="API-1")

    diff = compare_router.get_diff_rows(batch_id="API-1", link_code="ATTR-PRECISION")
    assert diff["precision"] == 2
    assert diff["coverage"]["比较覆盖触点数"] == len(diff["items"])
    assert diff["coverage"]["单侧待补触点数"] == 3
    tiny = next(row for row in diff["items"] if row["触点类型"] == "信息流广告")
    assert (tiny["首次归因"], tiny["末次归因"], tiny["差异"]) == (0.01, 0.01, 0.0)
    assert tiny["差异"] == round(abs(tiny["首次归因"] - tiny["末次归因"]), 2)

    claim = compare_router.get_claimable(link_code="ATTR-PRECISION")
    assert claim["可领条数"] == len(claim["items"])
    assert claim["待补报条数"] == 3

    settled = _asdict(compare_router.settle({
        "触点类型": ["信息流广告"], "batch_id": "API-1", "销账人": "白班A",
    }))
    assert settled["ok"] is True
    assert settled["entry"]["已销账"] == ["信息流广告"]
    assert settled["entry"]["按分差额合计"] == 0.0

    # 交班中断后补数，再提交原批：同一批不新增行，销账状态保留。
    run_attribution_compare_pipeline("ATTR-PRECISION", late_batch=True, batch_id="API-1")
    diff_after = compare_router.get_diff_rows(batch_id="API-1", link_code="ATTR-PRECISION")
    assert len(diff_after["items"]) == len(diff["items"])
    search = next(row for row in diff_after["items"] if row["触点类型"] == "搜索广告")
    assert search["可比状态"] == "双方可比"
    assert search["差异"] == 0.0

    claim_after = compare_router.get_claimable(link_code="ATTR-PRECISION")
    items = {row["触点类型"]: row for row in claim_after["items"]}
    # 末次金额被补报推高：旧销账结论作废，回到待重新核对。
    assert items["信息流广告"]["已销账"] is False
    assert items["信息流广告"]["需复核"] is True
    assert items["搜索广告"]["已销账"] is False

    replay = _asdict(compare_router.settle({
        "触点类型": ["搜索广告"], "batch_id": "API-1",
    }))["entry"]
    assert replay["已销账"] == ["搜索广告"]
    assert replay["按分差额合计"] == 0.0

    again = _asdict(compare_router.settle({
        "触点类型": ["搜索广告"], "batch_id": "API-1",
    }))["entry"]
    assert again["已销账"] == []
    assert again["重复销账"][0]["触点类型"] == "搜索广告"

    report = compare_router.get_report(batch_id="API-1", link_code="ATTR-PRECISION")
    assert report["比较覆盖触点数"] == len(diff_after["items"])
    assert report["已确认差异触点数"] == len(report["差异清单"])
    assert report["已确认差异合计"] == round(
        sum(row["差异"] for row in report["差异清单"]), 2)
    assert "搜索广告" in report["对账"]["已销账触点"]


def test_diff_rows_reject_non_comparable_settlement():
    run_attribution_compare_pipeline("ATTR-PENDING", batch_id="API-2")
    result = _asdict(compare_router.settle({"触点类型": ["搜索广告"], "batch_id": "API-2"}))
    assert result["entry"]["已销账"] == []
    assert result["entry"]["不可销账"][0]["原因"] == "一侧补报未到，暂不可销账"


def test_empty_batches_explain_reason():
    run_attribution_compare_pipeline("ATTR-NOTRADE", batch_id="API-3")
    diff = compare_router.get_diff_rows(batch_id="API-3", link_code="ATTR-NOTRADE")
    assert diff["items"] == []
    assert diff["empty_reason"].startswith("整批没有交易")
    claim = compare_router.get_claimable(link_code="ATTR-NOTRADE")
    assert claim["items"] == []
    assert claim["empty_reason"].startswith("整批没有交易")


def test_pipeline_trigger_requires_existing_link():
    result = _asdict(pipeline_router.trigger_attribution_compare("ATTR-NONE", False, None))
    assert result["ok"] is False
    assert result["message"] == "链路无数据"


def test_unknown_batch_returns_empty_not_stale():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="API-4")
    diff = compare_router.get_diff_rows(batch_id="API-404", link_code="ATTR-PRECISION")
    assert diff["items"] == []
    assert diff["coverage"]["比较覆盖触点数"] == 0
