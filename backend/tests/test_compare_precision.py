"""对比差异口径：按分发布、可按正式金额复算、补报重提幂等。"""
from __future__ import annotations

from app.services.compare_job import CompareJobService
from app.services.pipeline_orchestrator import run_attribution_compare_pipeline
from app.store import store


def _rows(link_code: str) -> dict[str, dict]:
    return {row["触点类型"]: row for row in CompareJobService().published_rows(link_code=link_code)}


def test_diff_recomputable_from_published_amounts():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    rows = _rows("ATTR-PRECISION")

    for row in rows.values():
        if row["可比状态"] != "双方可比":
            assert row["差异"] is None
            continue
        expected = abs(row["首次归因"] - row["末次归因"])
        assert row["差异"] == round(expected, 2)
        # 发布金额和差异都落在同一精度上：两位小数，没有浮点尾差。
        for key in ("首次归因", "末次归因", "差异"):
            assert row[key] == round(row[key], 2)


def test_tiny_amounts_round_to_same_cent_without_phantom_diff():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    rows = _rows("ATTR-PRECISION")

    # 原始值 0.014 / 0.005：两侧都发布为 0.01，差异必须是 0.00。
    assert rows["信息流广告"]["首次归因"] == 0.01
    assert rows["信息流广告"]["末次归因"] == 0.01
    assert rows["信息流广告"]["差异"] == 0.0

    # 两侧都观测到、都不到半分：发布为 0.00 且不是「没有数据」。
    for touch in ("直播", "短视频"):
        assert rows[touch]["首次归因"] == 0.0
        assert rows[touch]["末次归因"] == 0.0
        assert rows[touch]["首次有数据"] is True
        assert rows[touch]["末次有数据"] is True
        assert rows[touch]["可比状态"] == "双方可比"


def test_unobserved_side_is_null_not_zero():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    rows = _rows("ATTR-PRECISION")

    assert rows["开屏广告"]["首次归因"] == 0.02
    assert rows["开屏广告"]["末次归因"] is None
    assert rows["开屏广告"]["差异"] is None
    assert rows["开屏广告"]["已确认差异"] is False


def test_pipeline_result_and_diff_rows_agree():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    result = next(
        item["payload"] for item in store.rows("_pipeline_results")
        if item.get("pipeline_id") == "attribution_metric_compare"
    )
    pipeline_rows = {row["触点类型"]: row for row in result["compare_rows"]}
    published = _rows("ATTR-PRECISION")

    assert set(pipeline_rows) == set(published)
    for touch, row in published.items():
        for key in ("首次归因", "末次归因", "差异", "可比状态", "已确认差异"):
            assert pipeline_rows[touch][key] == row[key]


def test_totals_derived_from_published_rows():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    result = next(
        item["payload"] for item in store.rows("_pipeline_results")
        if item.get("pipeline_id") == "attribution_metric_compare"
    )
    rows = _rows("ATTR-PRECISION")

    first_sum = round(sum(r["首次归因"] for r in rows.values() if r["首次归因"] is not None), 2)
    last_sum = round(sum(r["末次归因"] for r in rows.values() if r["末次归因"] is not None), 2)
    diff_sum = round(sum(r["差异"] for r in rows.values() if r["差异"] is not None), 2)

    assert result["totals"]["first_touch"] == first_sum
    assert result["totals"]["last_touch"] == last_sum
    assert result["coverage"]["已发布差异合计"] == diff_sum


def test_late_batch_recomputes_same_batch_without_duplicate_rows():
    service = CompareJobService()
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    before = service.published_rows("P-1", "ATTR-PRECISION")
    assert _rows("ATTR-PRECISION")["搜索广告"]["末次归因"] is None

    run_attribution_compare_pipeline("ATTR-PRECISION", late_batch=True, batch_id="P-1")
    after = service.published_rows("P-1", "ATTR-PRECISION")

    assert len(after) == len(before)  # 补报只更新已有触点，不新增重复行
    assert _rows("ATTR-PRECISION")["搜索广告"]["末次归因"] == 0.2
    assert _rows("ATTR-PRECISION")["搜索广告"]["差异"] == 0.0

    # 再次提交原批：不新增重复行，金额保持一致。
    run_attribution_compare_pipeline("ATTR-PRECISION", late_batch=True, batch_id="P-1")
    replay = service.published_rows("P-1", "ATTR-PRECISION")
    assert len(replay) == len(after)


def test_resubmit_keeps_settled_rows_and_does_not_double_settle():
    service = CompareJobService()
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    first = service.settle(["信息流广告"], settled_by="白班A", batch_id="P-1")
    assert first["已销账"] == ["信息流广告"]

    run_attribution_compare_pipeline("ATTR-PRECISION", late_batch=True, batch_id="P-1")
    claimable = {item["触点类型"]: item for item in service.claimable("ATTR-PRECISION")["items"]}
    # 补报让信息流广告的末次金额从 0.01 变成 0.11，旧销账作废，必须重新核对。
    assert claimable["信息流广告"]["末次归因"] == 0.11
    assert claimable["信息流广告"]["差异"] == 0.1
    assert claimable["信息流广告"]["需复核"] is True

    replay = service.settle(["信息流广告"], settled_by="白班B", batch_id="P-1")
    assert replay["已销账"] == ["信息流广告"]
    assert replay["重复销账"] == []

    # 金额没再变化时，重复提交才是真正的重复销账。
    twice = service.settle(["信息流广告"], settled_by="白班B", batch_id="P-1")
    assert twice["已销账"] == []
    assert twice["重复销账"][0]["触点类型"] == "信息流广告"

    claimable = service.claimable("ATTR-PRECISION")
    settled = {item["触点类型"]: item for item in claimable["items"]}
    assert settled["信息流广告"]["已销账"] is True
    assert settled["搜索广告"]["已销账"] is False


def test_single_side_batch_cannot_settle_and_explains_why():
    service = CompareJobService()
    run_attribution_compare_pipeline("ATTR-PENDING", batch_id="Q-1")
    result = service.settle(["搜索广告"], settled_by="白班A", batch_id="Q-1")
    assert result["已销账"] == []
    assert result["不可销账"][0]["原因"] == "一侧补报未到，暂不可销账"
    assert service.claimable("ATTR-PENDING")["empty_reason"].startswith("一侧补报未到")


def test_no_trade_batch_explains_empty_and_reconcile_explains_all_settled():
    service = CompareJobService()
    run_attribution_compare_pipeline("ATTR-NOTRADE", batch_id="N-1")
    claimable = service.claimable("ATTR-NOTRADE")
    assert claimable["items"] == []
    assert claimable["empty_reason"].startswith("整批没有交易")

    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    names = [item["触点类型"] for item in service.claimable("ATTR-PRECISION")["items"]]
    service.settle(names, settled_by="白班A", batch_id="P-1")
    assert service.claimable("ATTR-PRECISION")["empty_reason"].startswith("金额已核平")


def test_report_splits_coverage_from_confirmed_diff():
    service = CompareJobService()
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    report = service.report_summary("P-1", "ATTR-PRECISION")

    assert report["比较覆盖触点数"] == len(service.published_rows("P-1", "ATTR-PRECISION"))
    assert report["已确认差异触点数"] == len(report["差异清单"])
    assert report["比较覆盖触点数"] > report["已确认差异触点数"]
    assert report["已确认差异合计"] == round(sum(row["差异"] for row in report["差异清单"]), 2)


def test_repeated_precision_stays_stable():
    run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    first = _rows("ATTR-PRECISION")
    for _ in range(3):
        run_attribution_compare_pipeline("ATTR-PRECISION", batch_id="P-1")
    again = _rows("ATTR-PRECISION")
    assert first == again
