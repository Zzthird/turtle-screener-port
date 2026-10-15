"""数据层 — 在业务代码与数据 API 之间加一层防腐层。

用法：
    from datasource import get, compare

    r = get("利润表", code="600887.SH", years=5, source="tushare")
    r = get("利润表", code="600887.SH", years=5, source="zzshare")

    diff = compare("利润表", "600887.SH", ["tushare", "zzshare"])

设计：
    业务代码 → datasource.get() → Adapter → Tushare / zzshare / AkShare
                      ↑
              切源只改这一层，业务代码零改动
"""

from __future__ import annotations

from .schema import (  # noqa: F401
    Value,
    Record,
    Result,
    Dataset,
    FieldSpec,
    DATASETS,
    get_dataset,
    UNIT_BASE,
)
from .base import DataSource, DataSourceError, register, get_adapter, list_sources  # noqa: F401
from .mapper import map_record, to_business_name, to_source_name  # noqa: F401
from .api import get, compare, crosscheck  # noqa: F401

__all__ = [
    "Value",
    "Record",
    "Result",
    "Dataset",
    "FieldSpec",
    "DATASETS",
    "get_dataset",
    "UNIT_BASE",
    "DataSource",
    "DataSourceError",
    "register",
    "get_adapter",
    "list_sources",
    "map_record",
    "to_business_name",
    "to_source_name",
    "get",
    "compare",
    "crosscheck",
]
