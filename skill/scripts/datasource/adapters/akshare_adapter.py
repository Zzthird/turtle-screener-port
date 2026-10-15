"""AkShare 适配器 — 基于 2026-10 实测。

实测确认：
  - AkShare **已是累计口径**，无需累加（2024 营业总收入 1157.80 亿 = 官方）
  - 数值单位 = 元
  - 三后端互补：
      新浪源 → 三大报表（中文列名）
      东财源 → 现金流量表（英文列名，含 D&A / 资本开支 / 已付股息）
      同花顺源 → 备用
  - **东财 push2*.eastmoney.com 被 IP 级风控 → 必须自动降级**

⚠️ AkShare 是爬虫集合，接口会随上游改版失效。所有调用均异常包装。
"""

from __future__ import annotations

from typing import Optional

from ..base import DataSource, DataSourceError, register
from . import akshare_fields as AF


def _strip_prefix(code: str) -> str:
    """sh600887 / 600887.SH -> 600887"""
    return code.split(".")[0].replace("sh", "").replace("sz", "").replace("bj", "")


def _to_sina(code: str) -> str:
    """600887.SH -> sh600887"""
    if "." not in code:
        return "sh" + code if code.startswith("6") else "sz" + code
    num, mkt = code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt.upper(), "sh") + num


def _to_em(code: str) -> str:
    """600887.SH -> SH600887"""
    if "." not in code:
        mkt = "SH" if code.startswith("6") else "SZ"
        return mkt + code
    num, mkt = code.split(".")
    return mkt.upper() + num


@register
class AkshareAdapter(DataSource):
    name = "akshare"
    priority = 30
    supports_hk = True
    supports_us = False

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
    def fetch(self, api: str, code: str, *, periods=None, limit: int = 100, **kwargs) -> list[dict]:
        handler = getattr(self, "_fetch_" + api, None)
        if handler is None:
            raise DataSourceError(f"akshare 未映射 Tushare 接口 {api!r}")
        return handler(code, periods, limit, **kwargs)

    def field_overrides(self, dataset_name: str, rows: list[dict]) -> dict[str, str]:
        """根据实际返回行动态切换字段映射。"""
        if not rows:
            return {}
        r0 = rows[0]
        keys = set(r0.keys())

        if dataset_name == "现金流量表" and "NETCASH_OPERATE" in keys:
            return dict(AF.CASHFLOW_EM_MAP)
        if dataset_name == "分红" and "派息" in keys:
            return dict(AF.DIVIDEND_MAP)
        if dataset_name == "股权质押" and "质押比例" in keys:
            return dict(AF.PLEDGE_MAP)
        if dataset_name == "十大股东" and "股东名称" in keys:
            return dict(AF.HOLDER_MAP)
        if dataset_name == "无风险利率" and "中国国债收益率10年" in keys:
            return dict(AF.RISKFREE_MAP)
        if dataset_name == "股份回购" and any("回购" in str(k) for k in keys):
            return dict(AF.REPURCHASE_MAP)

        # 行情：东财源中文列 或 腾讯源英文列
        if dataset_name in ("日线行情", "周线行情"):
            if "收盘" in keys:
                return dict(AF.PRICE_HIST_MAP)
            if "close" in keys:
                return dict(AF.PRICE_hist_EN_MAP)
        return {}

    # -- 三大报表（新浪）------------------------------------------------
    def _fetch_income(self, code, periods, limit, **kw):
        return self._sina_report(code, "income", "利润表", AF.PERIOD_COL_SINA, periods, limit)

    def _fetch_balancesheet(self, code, periods, limit, **kw):
        return self._sina_report(code, "balancesheet", "资产负债表", AF.PERIOD_COL_SINA, periods, limit)

    def _sina_report(self, code, api, symbol, period_col, periods, limit) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_financial_report_sina(stock=_to_sina(code), symbol=symbol)
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare.{symbol}(新浪) 失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        rows = df.to_dict("records")
        rows = self._annualize(rows, period_col, periods)
        for r in rows:
            r["end_date"] = str(r.get(period_col, ""))
        return rows[:limit]

    # -- 现金流量表（东财优先 → 新浪降级）------------------------------
    def _fetch_cashflow(self, code, periods, limit, **kw) -> list[dict]:
        ak = self._get_ak()
        # 1) 东财（含 D&A / 资本开支 / 已付股息）
        try:
            df = ak.stock_cash_flow_sheet_by_report_em(symbol=_to_em(code))
            if df is not None and len(df):
                rows = self._annualize(df.to_dict("records"), AF.PERIOD_COL_EM, periods)
                for r in rows:
                    r["end_date"] = str(r.get(AF.PERIOD_COL_EM, ""))[:10].replace("-", "")
                return rows[:limit]
        except Exception:                             # noqa: BLE001
            pass
        # 2) 新浪降级（D&A 缺失）
        rows = self._sina_report(code, "cashflow", "现金流量表", AF.PERIOD_COL_SINA, periods, limit)
        return rows

    # -- 分红 -----------------------------------------------------------
    def _fetch_dividend(self, code, periods, limit, **kw) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_history_dividend_detail(symbol=_strip_prefix(code), indicator="分红")
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 分红失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        rows = df.to_dict("records")
        # 用公告日期推算财年（分红通常次年派发）
        for r in rows:
            ann = str(r.get("公告日期", ""))[:4]
            r["__report_year__"] = str(int(ann) - 1) + "1231" if ann.isdigit() else ""
            r["end_date"] = r["__report_year__"]
        return rows[:limit]

    # -- 股权质押 -------------------------------------------------------
    def _fetch_pledge_stat(self, code, periods, limit, **kw) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_gpzy_pledge_ratio_em()
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 质押失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        num = _strip_prefix(code)
        rows = [r for r in df.to_dict("records")
                if str(r.get("股票代码", "")).endswith(num)]
        for r in rows:
            r["end_date"] = str(r.get("交易日期", "")).replace("-", "")
        return rows[:limit]

    # -- 股份回购 -------------------------------------------------------
    def _fetch_repurchase(self, code, periods, limit, **kw) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_repurchase_em()
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 回购失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        num = _strip_prefix(code)
        rows = [r for r in df.to_dict("records")
                if str(r.get("股票代码", "")).endswith(num)]
        for r in rows:
            r["end_date"] = str(r.get("最新公告日期", ""))[:10].replace("-", "")
        return rows[:limit]

    # -- 十大股东 -------------------------------------------------------
    def _fetch_top10_holders(self, code, periods, limit, **kw) -> list[dict]:
        ak = self._get_ak()
        date = "20240630"
        if periods:
            p = str(periods[0]).replace("-", "")[:8]
            date = f"{p[:4]}0630" if p[4:8] != "1231" else p
        try:
            df = ak.stock_gdfx_top_10_em(symbol=_to_sina(code), date=date)
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 十大股东失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        rows = df.to_dict("records")
        for r in rows:
            r["end_date"] = date
        return rows[:limit]

    # -- 无风险利率 -----------------------------------------------------
    def _fetch_yc_cb(self, code, periods, limit, **kw) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.bond_zh_us_rate()
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 国债收益率失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []
        rows = df.tail(limit).to_dict("records")
        for r in rows:
            r["trade_date"] = str(r.get("日期", ""))[:10].replace("-", "")
        return rows

    # -- 每日指标（PE/PB/市值）------------------------------------------
    def _fetch_daily_basic(self, code, periods, limit, **kw) -> list[dict]:
        """PE/PB/市值。东财 push2 已实测被封，走 datacenter + 本地计算。

        降级策略：
          1) 东财 datacenter F10（未被封）→ 估值指标
          2) 本地计算：市值 = 最新收盘价 × 总股本
        """
        ak = self._get_ak()
        num = _strip_prefix(code)
        out: dict = {"ts_code": code, "trade_date": ""}

        # 1) 最新收盘价（腾讯/新浪源，不依赖 push2）
        close = None
        try:
            df = ak.stock_zh_a_hist_tx(symbol=_to_sina(code), adjust="")
            if df is not None and len(df):
                close = float(df.iloc[-1]["close"])
                out["trade_date"] = str(df.iloc[-1]["date"]).replace("-", "")
        except Exception:                             # noqa: BLE001
            try:
                df = ak.stock_zh_a_hist(symbol=num, period="daily", adjust="")
                if df is not None and len(df):
                    close = float(df.iloc[-1]["收盘"])
                    out["trade_date"] = str(df.iloc[-1]["日期"]).replace("-", "")
            except Exception:                         # noqa: BLE001
                pass
        out["close"] = close

        # 2) 总股本（从资产负债表取实收资本；单位股）
        shares = None
        try:
            bs = ak.stock_financial_report_sina(stock=_to_sina(code), symbol="资产负债表")
            if bs is not None and len(bs):
                col = "实收资本(或股本)"
                if col in bs.columns:
                    v = bs.iloc[0][col]
                    if v == v:
                        shares = float(v)
        except Exception:                             # noqa: BLE001
            pass
        out["total_share"] = shares

        # 3) 市值 = 收盘价 × 总股本（本地计算）
        if close is not None and shares:
            out["total_mv"] = close * shares
            out["circ_mv"] = out["total_mv"]

        # 4) PE/PB：从 zzshare 估值表已能取到，此处留空由多源补齐
        out["pe_ttm"] = None
        out["pb"] = None
        out["dv_ttm"] = None

        return [out] if (close is not None or shares) else []

    # -- 主营业务构成 ---------------------------------------------------
    def _fetch_fina_mainbz(self, code, periods, limit, **kw) -> list[dict]:
        """AkShare stock_zygc_em —— 主营构成（按产品/地区分类）。

        列: 股票代码 报告日期 分类类型 主营构成 主营收入 收入比例
            主营成本 成本比例 主营利润 利润比例 毛利率
        """
        ak = self._get_ak()
        try:
            df = ak.stock_zygc_em(symbol=_to_em(code))
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"akshare 主营构成失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []

        rows = df.to_dict("records")
        # 只取最新年报期、按产品分类
        years = sorted({str(r.get("报告日期", ""))[:4] for r in rows if r.get("报告日期")},
                       reverse=True)
        target = years[0] if years else None
        picked = [r for r in rows
                  if str(r.get("报告日期", ""))[:4] == target
                  and r.get("分类类型") == "按产品分类"]
        if not picked:
            picked = [r for r in rows if str(r.get("报告日期", ""))[:4] == target]
        for r in picked:
            r["end_date"] = target or ""
        return picked[:limit]

    # -- 行情（日线 / 周线）--------------------------------------------
    def _fetch_daily(self, code, periods, limit, **kw) -> list[dict]:
        return self._hist(code, "daily", limit)

    def _fetch_weekly(self, code, periods, limit, **kw) -> list[dict]:
        return self._hist(code, "weekly", limit)

    def _hist(self, code, period, limit) -> list[dict]:
        ak = self._get_ak()
        try:
            df = ak.stock_zh_a_hist(symbol=_strip_prefix(code), period=period, adjust="")
        except Exception as e:                        # noqa: BLE001
            # push2his 被封 → 试腾讯源
            try:
                df = ak.stock_zh_a_hist_tx(symbol=_to_sina(code), adjust="")
            except Exception:                         # noqa: BLE001
                raise DataSourceError(
                    f"akshare 行情失败（东财/腾讯均不可用）：{str(e)[:100]}"
                ) from e
        if df is None or len(df) == 0:
            return []
        rows = df.tail(limit).to_dict("records")
        for r in rows:
            r["trade_date"] = str(r.get("日期", r.get("date", "")))[:10].replace("-", "")
        return rows

    # -- 基础信息 -------------------------------------------------------
    def _fetch_stock_basic(self, code, periods, limit, **kw) -> list[dict]:
        """push2 被封，改用新浪/东财 datacenter 不依赖 push2 的入口。"""
        ak = self._get_ak()
        num = _strip_prefix(code)
        out: dict = {"ts_code": code, "symbol": num}
        try:
            df = ak.stock_individual_basic_info_xq(symbol=_to_em(code))
            for r in df.to_dict("records"):
                out[r.get("item")] = r.get("value")
        except Exception:                             # noqa: BLE001
            pass
        out["end_date"] = ""
        return [out] if len(out) > 2 else []

    # -- 工具 -----------------------------------------------------------
    @staticmethod
    def _annualize(rows: list[dict], period_col: str, periods) -> list[dict]:
        """AkShare 已是累计口径，只保留年报（12-31）。"""
        def p8(r):
            return str(r.get(period_col, "")).replace("-", "").replace(" 00:00:00", "")[:8]

        annual = [r for r in rows if len(p8(r)) >= 8 and p8(r)[4:8] == "1231"]
        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
            picked = [r for r in annual if p8(r) in wanted]
            return picked or annual
        return annual
