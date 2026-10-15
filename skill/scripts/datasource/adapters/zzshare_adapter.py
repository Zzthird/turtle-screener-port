"""zzshare 适配器 — 基于 2026-10 实测。

实测确认：
  - finance_stock(table=, code=, start_date=, end_date=) 是单股历史查询
  - 利润表/现金流/指标返回**单季度值** → 需按年累加
  - 资产负债表是时点值 → 不累加，取期末
  - 字段名见 zzshare_fields.py（与 Tushare 完全不同）

实测验证（600887.SH 2024）：
  单季累加 → 营收 1153.93 亿 / 归母 84.53 亿
  官方年报 → 营收 1157.80 亿 / 归母 84.53 亿   ← 归母完全吻合

⚠️ zzshare 为个人项目，长期稳定性未经验证。所有调用均做异常包装。
"""

from __future__ import annotations

from typing import Optional

from ..base import DataSource, DataSourceError, register
from . import zzshare_fields as ZF


@register
class ZzshareAdapter(DataSource):
    name = "zzshare"
    priority = 20
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
        if self._api is None:
            try:
                from zzshare.client import DataApi
            except ImportError as e:
                raise DataSourceError("未安装 zzshare：pip install zzshare") from e
            self._api = DataApi()          # 匿名模式即可
        return self._api

    # -- 主入口 ---------------------------------------------------------
    def fetch(
        self,
        api: str,
        code: str,
        *,
        periods: Optional[list[str]] = None,
        limit: int = 100,
        **kwargs,
    ) -> list[dict]:
        if api in ZF.TABLE_MAP:
            return self._fetch_financial(api, code, periods, limit)
        if api in {"daily", "weekly"}:
            return self._fetch_price(api, code, limit, **kwargs)
        if api == "stock_basic":
            return self._fetch_basic(code)
        raise DataSourceError(f"zzshare 未映射 Tushare 接口 {api!r}")

    # -- 财务 -----------------------------------------------------------
    def _fetch_financial(self, api: str, code: str, periods, limit: int) -> list[dict]:
        zapi = self._get_api()
        table = ZF.TABLE_MAP[api]

        start = "2015-01-01"
        end = "2030-12-31"
        if periods:
            ps = sorted(str(p).replace("-", "") for p in periods)
            start = f"{ps[0][:4]}-01-01"
            end = f"{ps[-1][:4]}-12-31"

        if api == "daily_basic":
            return self._fetch_valuation(zapi, code, start, end, limit)

        try:
            df = zapi.finance_stock(
                table=table, code=code, start_date=start, end_date=end
            )
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"zzshare.finance_stock({table}) 失败：{str(e)[:150]}") from e

        if df is None or len(df) == 0:
            return []

        rows = df.to_dict("records")
        if api in ZF.QUARTERLY_CUMULATIVE:
            rows = self._accumulate_annual(rows)
        else:
            rows = [{"statDate": r.get("statDate"), **r} for r in rows
                    if str(r.get("statDate", "")).endswith("12-31")]

        # 补上 Tushare 风格的 period 字段（适配器 -> mapper 用）
        for r in rows:
            sd = str(r.get("statDate") or r.get("end_date") or "")
            r["end_date"] = sd.replace("-", "") or None
            r["statDate"] = sd

        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
            rows = [r for r in rows
                    if str(r.get("statDate", "")).replace("-", "") in wanted] or rows
        return rows[:limit]

    @staticmethod
    def _accumulate_annual(rows: list[dict]) -> list[dict]:
        """把单季度值按年累加为年度值。

        实测依据：zzshare finance_income 返回单季
          2024Q4 营收 266.60 亿（单季）
          2024 四季累加 = 1153.93 亿 ≈ 年报 1157.80 亿
        """
        buckets: dict[str, dict] = {}
        num_keys: set[str] = set()
        for r in rows:
            for k, v in r.items():
                if isinstance(v, (int, float)) and k not in {"statDate"}:
                    num_keys.add(k)

        for r in rows:
            sd = str(r.get("statDate", ""))
            if len(sd) < 4:
                continue
            y = sd[:4]
            b = buckets.setdefault(y, {"statDate": f"{y}-12-31", "pubDate": r.get("pubDate")})
            for k in num_keys:
                v = r.get(k)
                if isinstance(v, (int, float)) and v == v:      # skip NaN
                    b[k] = (b.get(k) or 0.0) + float(v)
                elif k not in b:
                    b[k] = None

        return [buckets[y] for y in sorted(buckets, reverse=True)]

    def _fetch_valuation(self, zapi, code: str, start: str, end: str, limit: int) -> list[dict]:
        """zzshare 估值表单位为**亿元/万股**，需转成元/股 以对齐 schema。"""
        try:
            df = zapi.finance_stock(
                table="valuation", code=code, start_date=start, end_date=end
            )
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"zzshare valuation 失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []

        rows = df.to_dict("records")
        # 亿元 -> 元（×1e8）；万股 -> 股（×1e4）
        SCALE = {
            "market_cap": 1e8, "circulating_market_cap": 1e8,
            "capitalization": 1e4, "circulating_cap": 1e4,
        }
        for r in rows:
            for k, mul in SCALE.items():
                v = r.get(k)
                if isinstance(v, (int, float)) and v == v:
                    r[k] = v * mul
        return rows[:limit]

    # -- 行情 -----------------------------------------------------------
    def _fetch_price(self, api: str, code: str, limit: int, **kwargs) -> list[dict]:
        zapi = self._get_api()
        try:
            df = zapi.daily(ts_code=code, limit=min(limit, 1000))
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"zzshare.daily 失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        return df.to_dict("records")[:limit]

    # -- 基础信息 -------------------------------------------------------
    def _fetch_basic(self, code: str) -> list[dict]:
        zapi = self._get_api()
        try:
            df = zapi.stock_basic(ts_code=code, fields="all")
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"zzshare.stock_basic 失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        return df.to_dict("records")
