"""数据层对外 API：get / compare / crosscheck。"""

from __future__ import annotations

from typing import Optional

from .base import DataSourceError, get_adapter, list_sources
from .schema import Record, Result, Value


def get(
    dataset: str,
    code: str,
    *,
    source: str = "auto",
    currency: str = "CNY",
    **kwargs,
) -> Result:
    """取数据集。

    Args:
        dataset: 业务数据集名（如 "利润表"）
        code:    股票代码（如 "600887.SH"）
        source:  "auto" 或指定源名；auto 按 priority 降级
        kwargs:  透传给适配器（periods / limit 等）
    """
    if source != "auto":
        return get_adapter(source).fetch_dataset(dataset, code, **kwargs)

    errors: list[str] = []
    for name in list_sources(only_available=True):
        try:
            r = get_adapter(name).fetch_dataset(dataset, code, **kwargs)
            if len(r) > 0:
                if errors:
                    r.warnings.insert(0, f"auto 降级：跳过 {', '.join(errors)}")
                return r
            errors.append(f"{name}(空)")
        except Exception as e:                        # noqa: BLE001
            errors.append(f"{name}({type(e).__name__}: {str(e)[:60]})")

    raise DataSourceError(
        f"所有数据源均失败：{'; '.join(errors)}。已注册：{list_sources()}"
    )


def _key_of(rec: Record, dataset: str) -> Optional[str]:
    from .schema import get_dataset
    ds = get_dataset(dataset)
    return rec.get(ds.key_field).period


def compare(
    dataset: str,
    code: str,
    sources: Optional[list[str]] = None,
    *,
    fields: Optional[list[str]] = None,
    tolerance_pct: float = 1.0,
    **kwargs,
) -> list[dict]:
    """多源对比：返回每个字段的偏差报告。

    Args:
        tolerance_pct: 相对偏差阈值（%），超过则标记 difference=True

    Returns:
        [{字段, 期间, <源名>: 值, 偏差pct, difference}, ...]
    """
    from .schema import get_dataset

    ds = get_dataset(dataset)
    srcs = sources or list_sources(only_available=True)
    if len(srcs) < 2:
        raise DataSourceError(f"对比至少需要 2 个可用源，当前：{srcs}")

    results: dict[str, Result] = {}
    for s in srcs:
        try:
            results[s] = get(dataset, code, source=s, **kwargs)
        except Exception as e:                        # noqa: BLE001
            results[s] = Result(dataset=dataset, source=s, warnings=[str(e)[:80]])

    # 以 (期间, 字段) 为键聚合
    buckets: dict[tuple, dict[str, float]] = {}
    for s, res in results.items():
        for rec in res:
            period = _key_of(rec, dataset)
            for spec in ds.fields:
                if fields and spec.key not in fields:
                    continue
                v: Value = rec.get(spec.key)
                mm = v.to_mm()
                if mm is None:
                    continue
                buckets.setdefault((period, spec.key), {})[s] = mm

    rows: list[dict] = []
    for (period, field), vals in sorted(buckets.items()):
        nums = [v for v in vals.values() if v is not None]
        if not nums:
            continue
        lo, hi = min(nums), max(nums)
        base = abs(lo) if abs(lo) > 1e-9 else None
        dev = ((hi - lo) / base * 100) if base else 0.0
        row = {"字段": field, "期间": period, "偏差pct": round(dev, 2),
               "difference": dev > tolerance_pct}
        for s in srcs:
            row[s] = round(vals[s], 2) if s in vals else None
        rows.append(row)

    return rows


def crosscheck(dataset: str, code: str, **kwargs) -> str:
    """对比并返回 Markdown 表格（人可读）。"""
    rows = compare(dataset, code, **kwargs)
    if not rows:
        return f"# {dataset} 多源对比 @ {code}\n\n无共同可比数据。\n"

    cols = [c for c in rows[0].keys()]
    lines = [f"# {dataset} 多源对比 @ {code}", ""]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "|".join(["---"] * len(cols)) + "|")
    for r in rows:
        cells = []
        for c in cols:
            v = r[c]
            if c == "difference":
                v = "⚠️ 是" if v else "否"
            cells.append("" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")

    bad = sum(1 for r in rows if r["difference"])
    lines += ["", f"**共 {len(rows)} 项，偏差超阈值 {bad} 项。**"]
    return "\n".join(lines)
