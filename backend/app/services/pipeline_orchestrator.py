"""跨模块数据管道编排器：实现 7 条真实数据流。

每条管道都读取上游模块的存储数据或 fixtures，执行真实计算，
把结果写入 store 的 _pipeline_results 命名空间，供下游消费。
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from app.fixtures import (
    ATTR_NOTRADE_TOUCHES,
    ATTR_PENDING_TOUCHES,
    ATTR_PRECISION_LATE_TOUCHES,
    ATTR_PRECISION_TOUCHES,
    ATTRIBUTION_TOUCHES,
    COHORT_USERS,
    DATA_MART_ROWS,
    DIMENSIONS,
    EXPERIMENT_RESULTS,
    LATE_ATTRIBUTION_TOUCHES,
    METRIC_DEFS,
    QUALITY_CHECK_SAMPLES,
    TREND_POINTS,
)
from app.services.attribution_engine import compute_attribution_models
from app.services.pipeline_results import list_results, read_result, write_result
from app.store import store

# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _write_result(pipeline_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把管道产物写入 store._pipeline_results，按 pipeline_id 幂等覆盖。"""
    return write_result(pipeline_id, payload)


def _read_result(pipeline_id: str) -> dict[str, Any] | None:
    return read_result(pipeline_id)


def _to_float(val: Any) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _to_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Pipeline 1: metric_def -> agg_task -> api_service
# ---------------------------------------------------------------------------

def run_metric_agg_pipeline(metric_code: str | None = None) -> dict[str, Any]:
    """读取指标定义与 fixtures 时序数据，按口径执行真实聚合，生成 API 产物。"""
    # 取指标定义（优先 fixture，再回退 store）
    defs = METRIC_DEFS
    if metric_code is None:
        target = defs[0]
    else:
        target = next((d for d in defs if d.get("指标编号") == metric_code), defs[0])

    formula = target.get("计算公式", "")
    granularity = target.get("聚合粒度", "日")

    points = list(TREND_POINTS)
    result: dict[str, Any] = {"metric_code": target.get("指标编号"), "granularity": granularity}

    if "sum(order_amount)" in formula or "gmv" in target.get("指标名称", "").lower():
        total = sum(p["gmv"] for p in points)
        result["value"] = round(total, _to_int(target.get("精度位数", 2)))
        result["unit"] = "元"
        result["point_count"] = len(points)
    elif "count(distinct user_id)" in formula or "uv" in target.get("指标名称", "").lower():
        total = sum(p["uv"] for p in points)
        result["value"] = total
        result["unit"] = "人"
        result["point_count"] = len(points)
    elif "paid_uv / total_uv" in formula or "转化率" in target.get("指标名称", ""):
        total_paid = sum(p["paid_uv"] for p in points)
        total_uv = sum(p["total_uv"] for p in points)
        cvr = total_paid / total_uv if total_uv else 0.0
        result["value"] = round(cvr, _to_int(target.get("精度位数", 4)))
        result["unit"] = "比率"
        result["point_count"] = len(points)
    elif "gmv / uv" in formula or "平均收入" in target.get("指标名称", ""):
        total_gmv = sum(p["gmv"] for p in points)
        total_uv = sum(p["uv"] for p in points)
        arpu = total_gmv / total_uv if total_uv else 0.0
        result["value"] = round(arpu, _to_int(target.get("精度位数", 2)))
        result["unit"] = "元/人"
        result["point_count"] = len(points)
    else:
        result["value"] = 0.0
        result["unit"] = "未知"
        result["point_count"] = 0

    # 写入 agg_task 与 api_service 可读产物
    _write_result("metric_agg_api", {
        "source_modules": ["metric_def", "agg_task"],
        "target_module": "api_service",
        "payload": result,
    })
    return result


# ---------------------------------------------------------------------------
# Pipeline 2: trend_series -> forecast -> report_gen
# ---------------------------------------------------------------------------

def run_forecast_pipeline(horizon: int = 7) -> dict[str, Any]:
    """基于 fixtures 时序数据做线性趋势外推，生成预测结果供报表消费。"""
    points = list(TREND_POINTS)
    n = len(points)
    if n < 2:
        return {"error": " insufficient data"}

    # 简单线性回归：x = 0..n-1, y = gmv
    xs = list(range(n))
    ys = [p["gmv"] for p in points]
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den = sum((x - x_mean) ** 2 for x in xs)
    slope = num / den if den else 0.0
    intercept = y_mean - slope * x_mean

    last_date = datetime.strptime(points[-1]["日期"], "%Y-%m-%d").date()
    forecasts: list[dict] = []
    for i in range(1, horizon + 1):
        pred = slope * (n - 1 + i) + intercept
        pred_date = last_date + timedelta(days=i)
        forecasts.append({
            "日期": pred_date.isoformat(),
            "预测gmv": round(pred, 2),
            "置信下界": round(pred * 0.92, 2),
            "置信上界": round(pred * 1.08, 2),
        })

    result = {
        "model": "linear_trend",
        "horizon": horizon,
        "slope": round(slope, 4),
        "intercept": round(intercept, 2),
        "last_actual": points[-1]["gmv"],
        "forecasts": forecasts,
    }

    _write_result("forecast_report", {
        "source_modules": ["trend_series", "forecast"],
        "target_module": "report_gen",
        "payload": result,
    })
    return result


# ---------------------------------------------------------------------------
# Pipeline 3: dim_registry -> cube_builder -> api_service
# ---------------------------------------------------------------------------

def run_cube_pipeline(dim_codes: list[str] | None = None) -> dict[str, Any]:
    """按指定维度组合 fixtures 数据集市行，生成多维立方体快照与查询接口产物。"""
    dims = DIMENSIONS
    if dim_codes is None:
        dim_codes = [d["维度编号"] for d in dims[:2]]

    # 映射维度编号到字段名
    dim_field_map = {
        "DIM-REGION": "region",
        "DIM-CHANNEL": "channel",
        "DIM-USER-TAG": "tag",
        "DIM-PRODUCT": "category_id",
    }
    selected_fields = [dim_field_map.get(dc) for dc in dim_codes if dim_field_map.get(dc)]

    rows = list(DATA_MART_ROWS)
    # 按选定维度分组聚合
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = tuple(r.get(f) for f in selected_fields)
        if key not in groups:
            groups[key] = {f: r.get(f) for f in selected_fields}
            groups[key]["订单数"] = 0
            groups[key]["gmv"] = 0.0
            groups[key]["uv"] = 0
        groups[key]["订单数"] += _to_int(r.get("订单数"))
        groups[key]["gmv"] += _to_float(r.get("gmv"))
        groups[key]["uv"] += _to_int(r.get("uv"))

    cells = []
    for g in groups.values():
        cells.append({k: round(v, 2) if isinstance(v, float) else v for k, v in g.items()})

    result = {
        "dimensions": dim_codes,
        "dim_fields": selected_fields,
        "cell_count": len(cells),
        "cells": cells,
    }

    _write_result("cube_api", {
        "source_modules": ["dim_registry", "cube_builder"],
        "target_module": "api_service",
        "payload": result,
    })
    return result


# ---------------------------------------------------------------------------
# Pipeline 4: cohort_rule -> data_extract -> report_gen
# ---------------------------------------------------------------------------

def run_cohort_extract_pipeline(rule: dict[str, Any] | None = None) -> dict[str, Any]:
    """按分群规则过滤 fixtures 用户，执行数据提取，生成分群报表产物。"""
    users = list(COHORT_USERS)
    if rule is None:
        rule = {"region": "华东", "min_ltv": 1000.0}

    filtered = []
    for u in users:
        match = True
        if "region" in rule and u.get("region") != rule["region"]:
            match = False
        if "channel" in rule and u.get("channel") != rule["channel"]:
            match = False
        if "tag" in rule and u.get("tag") != rule["tag"]:
            match = False
        if "min_ltv" in rule and _to_float(u.get("ltv")) < _to_float(rule["min_ltv"]):
            match = False
        if match:
            filtered.append(u)

    # 统计聚合
    total_ltv = sum(_to_float(u["ltv"]) for u in filtered)
    avg_ltv = total_ltv / len(filtered) if filtered else 0.0
    region_dist: dict[str, int] = {}
    for u in filtered:
        region_dist[u["region"]] = region_dist.get(u["region"], 0) + 1

    result = {
        "rule": rule,
        "matched_count": len(filtered),
        "users": filtered,
        "summary": {
            "total_ltv": round(total_ltv, 2),
            "avg_ltv": round(avg_ltv, 2),
            "region_distribution": region_dist,
        },
    }

    _write_result("cohort_extract_report", {
        "source_modules": ["cohort_rule", "data_extract"],
        "target_module": "report_gen",
        "payload": result,
    })
    return result


# ---------------------------------------------------------------------------
# Pipeline 5: experiment -> stat_model -> data_mart
# ---------------------------------------------------------------------------

def run_experiment_stat_pipeline(experiment_code: str | None = None) -> dict[str, Any]:
    """读取实验 fixtures，执行双比例 z 检验与效应量计算，产物写入数据集市。"""
    rows = list(EXPERIMENT_RESULTS)
    if experiment_code is None:
        experiment_code = "EXPE-001"

    exp_rows = [r for r in rows if r.get("实验编号") == experiment_code]
    if len(exp_rows) < 2:
        return {"error": "实验数据不足"}

    control = next((r for r in exp_rows if r.get("分组") == "control"), None)
    treatment = next((r for r in exp_rows if r.get("分组") == "treatment"), None)
    if control is None or treatment is None:
        return {"error": "缺少对照组或实验组"}

    n_c = _to_int(control.get("样本量"))
    n_t = _to_int(treatment.get("样本量"))
    conv_c = _to_int(control.get("转化人数"))
    conv_t = _to_int(treatment.get("转化人数"))

    p_c = conv_c / n_c if n_c else 0.0
    p_t = conv_t / n_t if n_t else 0.0
    p_pool = (conv_c + conv_t) / (n_c + n_t) if (n_c + n_t) else 0.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_c + 1 / n_t)) if (n_c + n_t) else 0.0
    z = (p_t - p_c) / se if se else 0.0

    # Cohen's h 效应量
    cohen_h = 2 * (math.asin(math.sqrt(p_t)) - math.asin(math.sqrt(p_c))) if (p_t >= 0 and p_c >= 0) else 0.0

    result = {
        "experiment_code": experiment_code,
        "control_rate": round(p_c, 4),
        "treatment_rate": round(p_t, 4),
        "absolute_lift": round(p_t - p_c, 4),
        "relative_lift": round((p_t - p_c) / p_c, 4) if p_c else None,
        "z_score": round(z, 4),
        "cohen_h": round(cohen_h, 4),
        "significant_at_05": abs(z) > 1.96,
        "sample_size_control": n_c,
        "sample_size_treatment": n_t,
    }

    # 同时写入 data_mart 可消费的统计摘要行
    mart_entry = {
        "主题编号": f"DATA-STAT-{experiment_code}",
        "region": "all",
        "channel": "all",
        "日期": str(date.today()),
        "实验编号": experiment_code,
        "控制组转化率": result["control_rate"],
        "实验组转化率": result["treatment_rate"],
        "相对提升": result["relative_lift"],
        "显著性": result["significant_at_05"],
    }
    store.rows("data_mart").append(mart_entry)

    _write_result("experiment_stat_mart", {
        "source_modules": ["experiment", "stat_model"],
        "target_module": "data_mart",
        "payload": result,
    })
    return result


# ---------------------------------------------------------------------------
# Pipeline 6: quality_mon 跨模块扫描
# ---------------------------------------------------------------------------

def run_quality_scan() -> dict[str, Any]:
    """扫描 fixtures 质量样本与 store 中各模块数据，生成跨模块质量报告。"""
    samples = list(QUALITY_CHECK_SAMPLES)
    issues: list[dict] = []
    total_rows = 0
    total_anomalies = 0

    for s in samples:
        anomaly = _to_int(s.get("异常行数"))
        total_anomalies += anomaly
        # 模拟总行数
        estimated = max(anomaly, 1000)
        total_rows += estimated
        if anomaly > 0:
            issues.append({
                "规则编号": s.get("规则编号"),
                "监控对象": s.get("监控对象"),
                "异常行数": anomaly,
                "空值率": s.get("空值率"),
                "重复率": s.get("重复率"),
            })

    # 扫描 store 各模块的 abnormal 标记
    module_issues: list[dict] = []
    for mod in store.module_names():
        if mod.startswith("_"):
            continue
        rows = store.rows(mod)
        abnormal_count = sum(1 for r in rows if r.get("abnormal"))
        if abnormal_count:
            module_issues.append({"module": mod, "abnormal_count": abnormal_count})

    score = max(0, 100 - int(total_anomalies / max(total_rows, 1) * 10000))
    result = {
        "scan_time": datetime.now().isoformat(),
        "total_rows_estimated": total_rows,
        "total_anomalies": total_anomalies,
        "quality_score": score,
        "sample_issues": issues,
        "module_issues": module_issues,
    }

    _write_result("quality_scan", {
        "source_modules": ["quality_mon"],
        "target_module": "_global",
        "payload": result,
    })
    return result


# ---------------------------------------------------------------------------
# Pipeline 7: attribution -> metric_def -> compare_job
# ---------------------------------------------------------------------------

ATTRIBUTION_LINKS: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {
    # 链路 -> (初始批次触点, 迟到补报触点)
    "ATTR-001": (ATTRIBUTION_TOUCHES, LATE_ATTRIBUTION_TOUCHES),
    "ATTR-002": (ATTRIBUTION_TOUCHES, []),
    "ATTR-003": (ATTRIBUTION_TOUCHES, []),
    "ATTR-PRECISION": (ATTR_PRECISION_TOUCHES, ATTR_PRECISION_LATE_TOUCHES),
    "ATTR-NOTRADE": (ATTR_NOTRADE_TOUCHES, []),
    "ATTR-PENDING": (ATTR_PENDING_TOUCHES, []),
}


def attribution_touches_for(
    link_code: str, late_batch: bool = False,
) -> list[dict[str, Any]]:
    """按链路取触点；``late_batch`` 决定是否并入迟到补报的那几条。"""
    base, late = ATTRIBUTION_LINKS.get(link_code, (ATTRIBUTION_TOUCHES, []))
    touches = list(base)
    if late_batch:
        touches.extend(late)
    return [t for t in touches if t.get("链路编号") == link_code]


def _publish_attribution_diff(
    link_code: str,
    touches: list[dict[str, Any]],
    *,
    batch_id: str,
    precision: int,
) -> dict[str, Any]:
    """把归因对比结果同步发到对比任务的差异清单。

    延迟 import 避免与 compare_job 形成循环依赖。
    """
    from app.services.compare_job import CompareJobService

    return CompareJobService().publish_diff_rows(
        link_code, touches, batch_id=batch_id, precision=precision)


def run_attribution_compare_pipeline(
    link_code: str | None = None,
    late_batch: bool = False,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """读取归因 fixtures，按触点类型做首次/末次/线性权重分配，生成指标对比产物。

    ``late_batch=True`` 时把补报触点一起并入计算；``batch_id`` 相同时重复
    执行只更新同一批差异行，不追加重复行，已销账状态也不会被冲掉。
    """
    if link_code is None:
        link_code = "ATTR-001"
    touches = attribution_touches_for(link_code, late_batch)

    precision = 2
    for d in METRIC_DEFS:
        if d.get("指标编号") == "METR-GMV":
            precision = _to_int(d.get("精度位数", 2))
            break

    result = compute_attribution_models(link_code, touches, precision=precision)
    if "error" in result:
        return result

    if batch_id is None:
        batch_id = f"{link_code}:{'late' if late_batch else 'initial'}"
    result["batch_id"] = batch_id
    result["late_batch_applied"] = bool(late_batch)
    result["source_touch_count"] = len([t for t in touches if t.get("链路编号") == link_code])

    # 差异清单与管道产物同批发布：差异行、覆盖口径、已发布合计一起可见。
    _publish_attribution_diff(link_code, touches, batch_id=batch_id, precision=precision)

    write_result("attribution_metric_compare", {
        "batch_id": batch_id,
        "link_code": link_code,
        "late_batch_applied": result["late_batch_applied"],
        "source_modules": ["attribution", "metric_def"],
        "target_module": "compare_job",
        "payload": result,
    })
    return result


def list_attribution_compare_batches(link_code: str | None = None) -> list[dict[str, Any]]:
    """读出已发布的归因对比批次，供对比任务按正式金额复算差异。"""
    rows = list_results("attribution_metric_compare")
    if link_code is not None:
        rows = [r for r in rows if r.get("link_code") == link_code]
    rows.sort(key=lambda r: str(r.get("batch_id", "")))
    return rows


# ---------------------------------------------------------------------------
# 批量入口
# ---------------------------------------------------------------------------

def run_all_pipelines() -> dict[str, Any]:
    """顺序执行全部 7 条管道并返回摘要。"""
    return {
        "metric_agg_api": run_metric_agg_pipeline(),
        "forecast_report": run_forecast_pipeline(),
        "cube_api": run_cube_pipeline(),
        "cohort_extract_report": run_cohort_extract_pipeline(),
        "experiment_stat_mart": run_experiment_stat_pipeline(),
        "quality_scan": run_quality_scan(),
        "attribution_metric_compare": run_attribution_compare_pipeline(),
    }


def list_pipeline_results(pipeline_id: str | None = None) -> list[dict[str, Any]]:
    return list_results(pipeline_id)
