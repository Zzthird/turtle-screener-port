"""字段映射：源字段名 <-> 业务语义名。

切源时只需改 `schema.py` 里 FieldSpec 的 tushare/zzshare/akshare 列，
本模块不需要改。
"""

from __future__ import annotations

from typing import Any, Optional

from .schema import (
    Dataset,
    FieldSpec,
    Record,
    SourceName,
    Value,
)


def to_source_name(dataset: Dataset, business_key: str, source: SourceName) -> Optional[str]:
    """业务名 → 该源的字段名。"""
    for f in dataset.fields:
        if f.key == business_key:
            return f.native(source)
    return None


def to_business_name(dataset: Dataset, source_key: str, source: SourceName) -> Optional[str]:
    """该源的字段名 → 业务名。"""
    for f in dataset.fields:
        if f.native(source) == source_key:
            return f.key
    return None


def _coerce(raw: Any, spec: FieldSpec) -> Optional[float]:
    """把原始值转 float。空值/非法值/文本类字段返回 None。"""
    if getattr(spec, "kind", "num") == "str":
        return None                     # 文本字段不参与数值
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return None if v != v else v          # NaN -> None
    s = str(raw).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "--", "n/a"}:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def map_record(
    raw: dict,
    dataset: Dataset,
    source: SourceName,
    *,
    currency: str = "CNY",
    overrides: Optional[dict[str, str]] = None,
) -> Record:
    """把一行原始数据映射为统一 schema 的 Record。

    overrides: {业务名: 该源实际字段名}，用于运行期覆盖（如 AkShare 双后端）。
    """
    import datetime as _dt

    overrides = overrides or {}
    now = _dt.datetime.now().isoformat(timespec="seconds")
    values: dict[str, Value] = {}

    # 期间：优先取主键字段的原生名（含 override），回退到常见日期列
    period = None
    key_field_name = dataset.key_field
    sk = overrides.get(key_field_name)
    if sk is None:
        for f in dataset.fields:
            if f.key == key_field_name:
                sk = f.native(source)
                break
    if sk and raw.get(sk) is not None:
        period = str(raw.get(sk))
    if period is None:
        for alt in ("end_date", "statDate", "trade_date", "date", "日期",
                    "公告日期", "交易日期", "报告日"):
            if raw.get(alt) is not None:
                period = str(raw.get(alt))
                break

    for spec in dataset.fields:
        src_key = overrides.get(spec.key) or spec.native(source)
        if spec.key == dataset.key_field and period is not None:
            # 主键字段：值不参与计算，只携带 period
            values[spec.key] = Value(
                1.0, unit=spec.unit, currency=currency, period=period,
                source=source, fetched_at=now, note="period-key",
            )
            continue

        if not src_key:
            values[spec.key] = Value(
                None, unit=spec.unit, currency=currency, period=period, source=source,
                fetched_at=now, note=f"{source} 无对应字段",
            )
            continue

        num = _coerce(raw.get(src_key), spec)
        txt = None
        note = None
        if getattr(spec, "kind", "num") == "str":
            raw_v = raw.get(src_key)
            txt = None if raw_v is None else str(raw_v).strip() or None
            note = None if txt else f"{source}.{src_key} 返回空值"
        else:
            note = None if num is not None else f"{source}.{src_key} 返回空值"

        values[spec.key] = Value(
            num,
            unit=spec.unit,
            currency=currency,
            period=period,
            source=source,
            fetched_at=now,
            note=note,
            text=txt,
        )
        # 保证 key_field 一定带 period（矩阵渲染依赖它）
        if spec.key == dataset.key_field and period:
            values[spec.key] = Value(
                1.0, unit=spec.unit, currency=currency, period=period,
                source=source, fetched_at=now, note="period-key",
            )

    return Record(values=values, source=source)


def map_records(
    raw_rows: list[dict],
    dataset: Dataset,
    source: SourceName,
    *,
    currency: str = "CNY",
    overrides: Optional[dict[str, str]] = None,
) -> list[Record]:
    return [map_record(r, dataset, source, currency=currency, overrides=overrides)
            for r in (raw_rows or [])]
