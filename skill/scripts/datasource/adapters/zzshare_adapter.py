"""zzshare 适配器。

zzshare 兼容 Tushare 的部分接口命名，但有差异：
  - 财务表：finance_income / finance_balance / finance_cash_flow / finance_indicator / finance_valuation
  - 参数是**报告期**（'2024q4' 或 '2024-12-31'），不是 Tushare 的 period
  - 无 Token 时匿名可用（频率较低）

⚠️ 警示：zzshare 是个人项目，长期稳定性未经独立验证。
   本适配器对其所有调用都做异常包装，失败时返回空列表让上层降级。
"""

from __future__ import annotations

import os
from typing import Optional

from ..base import DataSource, DataSourceError, register

# Tushare 接口名 -> zzshare 方法名
API_MAP = {
    "income":         "finance_income",
    "balancesheet":   "finance_balance",
    "cashflow":       "finance_cash_flow",
    "fina_indicator": "finance_indicator",
    "daily_basic":    "finance_valuation",     # 日频估值
    "daily":          "daily",
    "weekly":         "weekly",
    "stock_basic":    "stock_basic",
    "dividend":       "dividend",
    "trade_cal":      "trade_days",
}

# 报告期格式：Tushare '20241231' -> zzshare '2024-12-31'
def _to_zz_period(p: str) -> str:
    p = str(p).strip()
    if len(p) == 8 and p.isdigit():
        return f"{p[:4]}-{p[4:6]}-{p[6:]}"
    return p


@register
class ZzshareAdapter(DataSource):
    name = "zzshare"
    priority = 20                 # 次优先（免费，但港美股不支持）
    supports_hk = False
    supports_us = False

    def __init__(self):
        self._api = None

    def available(self) -> bool:
        try:
            import zzshare  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_api(self):
        if self._api is not None:
            return self._api
        try:
            from zzshare.client import DataApi
        except ImportError as e:
            raise DataSourceError("未安装 zzshare：pip install zzshare") from e
        tok = os.environ.get("ZZSHARE_TOKEN") or None
        self._api = DataApi(token=tok) if tok else DataApi()
        return self._api

    def fetch(
        self,
        api: str,
        code: str,
        *,
        periods: Optional[list[str]] = None,
        limit: int = 100,
        **kwargs,
    ) -> list[dict]:
        method_name = API_MAP.get(api)
        if not method_name:
            raise DataSourceError(f"zzshare 未映射 Tushare 接口 {api!r}")

        zapi = self._get_api()
        fn = getattr(zapi, method_name, None)
        if fn is None:
            raise DataSourceError(f"zzshare.{method_name} 不存在（版本可能不同）")

        out: list[dict] = []

        # 财务类：按报告期逐个取
        if periods:
            for p in periods:
                try:
                    df = fn(_to_zz_period(p))
                except Exception as e:                # noqa: BLE001
                    raise DataSourceError(
                        f"zzshare.{method_name}({p}) 失败：{str(e)[:120]}"
                    ) from e
                if df is not None and len(df):
                    rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
                    out.extend(self._filter_code(rows, code))
            return out

        # 行情类
        try:
            if method_name == "daily":
                df = fn(ts_code=code, limit=limit, **kwargs)
            else:
                df = fn(ts_code=code, **kwargs)
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"zzshare.{method_name} 失败：{str(e)[:120]}") from e

        if df is None or len(df) == 0:
            return []
        rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
        return self._filter_code(rows, code)[:limit]

    @staticmethod
    def _filter_code(rows: list[dict], code: str) -> list[dict]:
        """财报表返回全市场，需按代码过滤。"""
        if not rows or "ts_code" not in rows[0]:
            return rows
        return [r for r in rows if str(r.get("ts_code", "")) == code] or rows
