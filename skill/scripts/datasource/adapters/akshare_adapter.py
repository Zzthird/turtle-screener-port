"""AkShare 适配器。

AkShare 是**爬虫集合**，字段名与 Tushare 完全不同，且接口随上游改版会失效。
本适配器只覆盖框架必需的接口，缺失项返回空列表让上层降级。

⚠️ 风险：AkShare 接口可用性是动态的。任何一次调用失败都不应中断整体流程。
"""

from __future__ import annotations

from typing import Optional

from ..base import DataSource, DataSourceError, register


def _ts_to_ak(code: str) -> str:
    """600887.SH -> sh600887"""
    if "." not in code:
        return code
    num, mkt = code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}.get(mkt, mkt.lower()) + num


@register
class AkshareAdapter(DataSource):
    name = "akshare"
    priority = 30                 # 最后兜底
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

    def fetch(
        self,
        api: str,
        code: str,
        *,
        periods: Optional[list[str]] = None,
        limit: int = 100,
        **kwargs,
    ) -> list[dict]:
        ak = self._get_ak()
        akcode = _ts_to_ak(code)

        if api in {"income", "balancesheet", "cashflow"}:
            return self._fetch_financial(ak, akcode, api, periods)

        if api == "daily":
            df = ak.stock_zh_a_hist(symbol=akcode.replace("sh", "").replace("sz", ""),
                                    period="daily", adjust="")
            return df.tail(limit).to_dict("records") if df is not None and len(df) else []

        if api == "stock_basic":
            df = ak.stock_info_a_code_name()
            return df.to_dict("records") if df is not None and len(df) else []

        if api == "dividend":
            df = ak.stock_history_dividend_detail(symbol=akcode.replace("sh", "").replace("sz", ""),
                                                  indicator="分红")
            return df.to_dict("records") if df is not None and len(df) else []

        raise DataSourceError(f"akshare 未映射 Tushare 接口 {api!r}")

    def _fetch_financial(self, ak, akcode: str, api: str, periods: Optional[list[str]]) -> list[dict]:
        """三大报表走新浪源。"""
        name_map = {"income": "利润表", "balancesheet": "资产负债表", "cashflow": "现金流量表"}
        try:
            df = ak.stock_financial_report_sina(stock=akcode, symbol=name_map[api])
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare.{name_map[api]} 失败：{str(e)[:120]}") from e

        if df is None or len(df) == 0:
            return []

        # 新浪返回的是宽表（列=报告期），需要转成长期格式
        records = df.to_dict("records")
        if periods:
            wanted = {str(p).replace("-", "") for p in periods}
            records = [
                r for r in records
                if any(str(k).replace("-", "").startswith(w[:8]) for k in r.keys() for w in wanted)
            ] or records
        return records[:50]
