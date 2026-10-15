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
    """把原始值转 float。空值/非法值返回 None。"""
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
) -> Record:
    """把一行原始数据映射为统一 schema 的 Record。"""
    import datetime as _dt

    now = _dt.datetime.now().isoformat(timespec="seconds")
    values: dict[str, Value] = {}

    # 期间：优先取主键字段的原生名，回退到 end_date / statDate
    period = None
    for f in dataset.fields:
        if f.key == dataset.key_field:
            sk = f.native(source)
            if sk and raw.get(sk) is not None:
                period = str(raw.get(sk))
            break
    if period is None:
        for alt in ("end_date", "statDate", "trade_date", "date"):
            if raw.get(alt) is not None:
                period = str(raw.get(alt))
                break

    for spec in dataset.fields:
        src_key = spec.native(source)
        if not src_key:
            values[spec.key] = Value(
                None, unit=spec.unit, currency=currency, source=source,
                fetched_at=now, note=f"{source} 无对应字段",
            )
            continue

        num = _coerce(raw.get(src_key), spec)
        note = None if num is not None else f"{source}.{src_key} 返回空值"

        values[spec.key] = Value(
            num,
            unit=spec.unit,
            currency=currency,
            period=period,
            source=source,
            fetched_at=now,
            note=note,
        )

    return Record(values=values, source=source)


def map_records(
    raw_rows: list[dict],
    dataset: Dataset,
    source: SourceName,
    *,
    currency: str = "CNY",
) -> list[Record]:
    return [map_record(r, dataset, source, currency=currency) for r in (raw_rows or [])]
