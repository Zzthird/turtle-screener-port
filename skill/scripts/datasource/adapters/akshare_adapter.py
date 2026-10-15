"""AkShare 适配器 — 基于 2026-10 实测。

实测确认：
  - AkShare **已是累计口径**，无需累加（2024 营业总收入 1157.80 亿 = 官方）
  - 数值单位 = 元
  - 双后端互补：
      新浪源 → 三大报表（中文列名）
      东财源 → 现金流量表（英文列名，含 D&A / 资本开支 / 已付股息）

⚠️ AkShare 是爬虫集合，接口会随上游改版失效。所有调用均异常包装。
"""

from __future__ import annotations

from typing import Optional

from ..base import DataSource, DataSourceError, register
from . import akshare_fields as AF


def _strip_prefix(code: str) -> str:
    """sh600887 -> 600887"""
    c = code.split(".")[0]
    return c


def _to_sina(code: str) -> str:
    """600887.SH -> sh600887"""
    if "." not in code:
        return code
    num, mkt = code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt.upper(), "sh") + num


def _to_em(code: str) -> str:
    """600887.SH -> SH600887"""
    if "." not in code:
        return "SH" + code
    num, mkt = code.split(".")
    return mkt.upper() + num


@register
class AkshareAdapter(DataSource):
    name = "akshare"
    priority = 30
    supports_hk = True
    supports_us = True

    def __init__(self):
        self._ak = None

    def available(self) -> bool:
        try:
            import akshare  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_ak(self):
        if self._ak is None:
            try:
                import akshare as ak
            except ImportError as e:
                raise DataSourceError("未安装 akshare：pip install akshare") from e
            self._ak = ak
        return self._ak

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
        if api in {"income", "balancesheet"}:
            return self._fetch_sina(api, code, periods, limit)
        if api == "cashflow":
            return self._fetch_cashflow(code, periods, limit)
        if api == "daily":
            return self._fetch_daily(code, limit)
        if api == "stock_basic":
            return self._fetch_basic(code)
        raise DataSourceError(f"akshare 未映射 Tushare 接口 {api!r}")

    def field_overrides(self, dataset_name: str, rows: list[dict]) -> dict[str, str]:
        """现金流走东财源时，用英文列名覆盖 schema 里的新浪中文列名。"""
        if dataset_name == "现金流量表" and rows and "NETCASH_OPERATE" in rows[0]:
            return {k: v for k, v in AF.CASHFLOW_EM_MAP.items()}
        return {}

    # -- 新浪三大报表 ---------------------------------------------------
    def _fetch_sina(self, api: str, code: str, periods, limit: int) -> list[dict]:
        ak = self._get_ak()
        sym = {"income": "利润表", "balancesheet": "资产负债表"}[api]
        try:
            df = ak.stock_financial_report_sina(stock=_to_sina(code), symbol=sym)
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare.{sym} 失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []

        rows = df.to_dict("records")
        rows = self._filter_periods(rows, periods, AF.PERIOD_COL_SINA)
        for r in rows:
            r["end_date"] = str(r.get(AF.PERIOD_COL_SINA, ""))
        return rows[:limit]

    # -- 现金流量表（东财优先，含 D&A）---------------------------------
    def _fetch_cashflow(self, code: str, periods, limit: int) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_cash_flow_sheet_by_report_em(symbol=_to_em(code))
            rows = df.to_dict("records")
            rows = self._filter_periods(rows, periods, AF.PERIOD_COL_EM)
            for r in rows:
                pd_ = str(r.get(AF.PERIOD_COL_EM, ""))[:10]
                r["end_date"] = pd_.replace("-", "")
            return rows[:limit]
        except Exception as em_err:                   # noqa: BLE001
            # 降级到新浪源（无 D&A）
            try:
                df = ak.stock_financial_report_sina(stock=_to_sina(code), symbol="现金流量表")
                rows = df.to_dict("records")
                rows = self._filter_periods(rows, periods, AF.PERIOD_COL_SINA)
                for r in rows:
                    r["end_date"] = str(r.get(AF.PERIOD_COL_SINA, ""))
                return rows[:limit]
            except Exception as sina_err:             # noqa: BLE001
                raise DataSourceError(
                    f"akshare 现金流失败：东财({str(em_err)[:60]}) / 新浪({str(sina_err)[:60]})"
                ) from em_err

    @staticmethod
    def _filter_periods(rows: list[dict], periods, period_col: str) -> list[dict]:
        """AkShare 已是累计口径，只保留年报（12-31）。"""
        annual = []
        for r in rows:
            p = str(r.get(period_col, "")).replace("-", "").replace(" 00:00:00", "")
            if len(p) >= 8 and p[4:8] == "1231":
                annual.append(r)
        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
            filtered = [r for r in annual
                        if str(r.get(period_col, "")).replace("-", "").replace(" 00:00:00", "")[:8] in wanted]
            return filtered or annual
        return annual

    # -- 行情 -----------------------------------------------------------
    def _fetch_daily(self, code: str, limit: int) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_zh_a_hist(symbol=_strip_prefix(code), period="daily", adjust="")
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 日线失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        return df.tail(limit).to_dict("records")

    # -- 基础信息 -------------------------------------------------------
    def _fetch_basic(self, code: str) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_individual_info_em(symbol=_strip_prefix(code))
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 基础信息失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        return df.to_dict("records")
