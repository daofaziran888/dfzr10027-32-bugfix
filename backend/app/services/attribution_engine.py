"""多触点归因计算引擎：首次 / 末次 / 线性三种模型的金额分配。

这里只做纯计算，不读写 store，也不关心批次与销账，方便管道编排器与
对比任务共用同一份口径：金额先按原始值算，发布时统一四舍五入到分，
差异由两侧已发布金额相减得出，下游拿两侧金额即可复算。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def quantize(value: float, precision: int) -> float:
    """按给定精度做四舍五入（half-up），避免浮点尾差与银行家舍入。"""
    if precision < 0:
        precision = 0
    quantum = Decimal(1).scaleb(-precision)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_attribution_models(
    link_code: str,
    touches: list[dict[str, Any]],
    *,
    precision: int = 2,
) -> dict[str, Any]:
    """按首次/末次/线性三种模型分配触点金额，并按同一精度发布。"""
    link_touches = [t for t in touches if t.get("链路编号") == link_code]
    if not link_touches:
        return {"error": "链路无数据", "link_code": link_code}

    users: dict[str, list[dict[str, Any]]] = {}
    for t in link_touches:
        users.setdefault(str(t["user_id"]), []).append(t)

    first_touch: dict[str, float] = {}
    last_touch: dict[str, float] = {}
    linear_touch: dict[str, float] = {}
    # 记录每个触点类型在哪个模型里真正被观测到。某侧压根没有这个触点时，
    # 不能把它当成「观测到的 0 元」。
    observed: dict[str, set[str]] = {}
    converted_users = 0

    for _uid, ts in users.items():
        ts_sorted = sorted(ts, key=lambda x: int(x["触点序号"]))
        conv_amount = to_float(ts_sorted[-1].get("贡献金额"))
        if conv_amount <= 0:
            continue
        converted_users += 1
        touch_points = [t for t in ts_sorted if t["触点类型"] != "下单"]
        if not touch_points:
            continue

        ft = touch_points[0]["触点类型"]
        first_touch[ft] = first_touch.get(ft, 0.0) + conv_amount
        observed.setdefault(ft, set()).add("first_touch")

        lt = touch_points[-1]["触点类型"]
        last_touch[lt] = last_touch.get(lt, 0.0) + conv_amount
        observed.setdefault(lt, set()).add("last_touch")

        share = conv_amount / len(touch_points)
        for t in touch_points:
            linear_touch[t["触点类型"]] = linear_touch.get(t["触点类型"], 0.0) + share
            observed.setdefault(t["触点类型"], set()).add("linear")

    published_first = {k: quantize(v, precision) for k, v in first_touch.items()}
    published_last = {k: quantize(v, precision) for k, v in last_touch.items()}
    published_linear = {k: quantize(v, precision) for k, v in linear_touch.items()}

    all_types = sorted(set(observed) | set(published_first) | set(published_last) | set(published_linear))
    compare_rows: list[dict[str, Any]] = []
    for t in all_types:
        observed_models = observed.get(t, set())
        has_first = "first_touch" in observed_models
        has_last = "last_touch" in observed_models
        has_linear = "linear" in observed_models
        first_value = published_first.get(t) if has_first else None
        last_value = published_last.get(t) if has_last else None

        if has_first and has_last:
            # 差异只由两侧已发布金额相减得出，与两侧金额同一精度、可复算。
            diff: float | None = quantize(abs(float(first_value) - float(last_value)), precision)
            status = "双方可比"
            confirmed = diff > 0
        elif has_first:
            diff = None
            status = "仅首次归因待补报"
            confirmed = False
        elif has_last:
            diff = None
            status = "仅末次归因待补报"
            confirmed = False
        else:
            diff = None
            status = "仅线性归因待补报"
            confirmed = False

        compare_rows.append({
            "触点类型": t,
            "首次归因": first_value,
            "末次归因": last_value,
            "差异": diff,
            "首次有数据": has_first,
            "末次有数据": has_last,
            "线性有数据": has_linear,
            "可比状态": status,
            "在比较范围内": has_first or has_last or has_linear,
            "已确认差异": confirmed,
        })

    confirmed_rows = [row for row in compare_rows if row["已确认差异"]]
    reconciled_rows = [
        row for row in compare_rows if row["可比状态"] == "双方可比" and not row["已确认差异"]
    ]
    pending_rows = [row for row in compare_rows if row["可比状态"] != "双方可比"]

    return {
        "link_code": link_code,
        "precision": precision,
        "user_count": len(users),
        "converted_user_count": converted_users,
        "models": {
            "first_touch": published_first,
            "last_touch": published_last,
            "linear": published_linear,
        },
        # 合计由已发布的触点行相加得出，下游拿发布行就能复算。
        "totals": {
            "first_touch": quantize(sum(published_first.values()), precision),
            "last_touch": quantize(sum(published_last.values()), precision),
            "linear": quantize(sum(published_linear.values()), precision),
        },
        "compare_rows": compare_rows,
        "coverage": {
            "比较覆盖触点数": len(compare_rows),
            "已确认差异触点数": len(confirmed_rows),
            "已核平触点数": len(reconciled_rows),
            "单侧待补触点数": len(pending_rows),
            "已发布差异合计": quantize(sum(float(row["差异"]) for row in confirmed_rows), precision),
        },
    }
