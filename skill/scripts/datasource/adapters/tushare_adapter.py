"""Tushare 适配器 — 包装现有 TushareClient，不改变其行为。

现有代码位于 scripts/tushare_modules/，本适配器只是薄包装。
"""

from __future__ import annotations

import os
from typing import Optional

from ..base import DataSource, DataSourceError, register


@register
class TushareAdapter(DataSource):
    name = "tushare"
    priority = 10                 # 最优先（字段最全 + 支持港美股）
    supports_hk = True
    supports_us = True

    def __init__(self):
        self._client = None

    def available(self) -> bool:
        if os.environ.get("TUSHARE_TOKEN"):
            return True
        # 尝试从 .env 读
        try:
            import sys, pathlib
            root = pathlib.Path(__file__).resolve().parents[3]
            env = root / ".env"
            if env.exists():
                for line in env.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("TUSHARE_TOKEN="):
                        tok = line.split("=", 1)[1].strip().strip("'\"")
                        if tok:
                            os.environ["TUSHARE_TOKEN"] = tok
                            return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import tushare as ts
        except ImportError as e:
            raise DataSourceError("未安装 tushare：pip install tushare") from e

        tok = os.environ.get("TUSHARE_TOKEN")
        if not tok:
            raise DataSourceError("TUSHARE_TOKEN 未配置")
        self._client = ts.pro_api(tok)
        return self._client

    def fetch(
        self,
        api: str,
        code: str,
        *,
        periods: Optional[list[str]] = None,
        limit: int = 100,
        **kwargs,
    ) -> list[dict]:
        pro = self._get_client()

        params = dict(kwargs)
        params.setdefault("ts_code", code)

        # 财务类接口用 period 精确取；行情类用日期区间
        if periods:
            out: list[dict] = []
            for p in periods:
                try:
                    df = pro.query(api, period=p, **params)
                except Exception as e:                # noqa: BLE001
                    raise DataSourceError(f"tushare.{api} 失败：{str(e)[:120]}") from e
                if df is not None and len(df):
                    out.extend(df.to_dict("records"))
            return out

        try:
            df = pro.query(api, **params)
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"tushare.{api} 失败：{str(e)[:120]}") from e
        if df is None or len(df) == 0:
            return []
        return df.head(limit).to_dict("records")
