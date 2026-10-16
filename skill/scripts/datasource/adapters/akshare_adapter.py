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

import re
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
            # 腾讯源英文列
            if "close" in keys and "open" in keys:
                m = dict(AF.PRICE_hist_EN_MAP)
            else:
                m = dict(AF.PRICE_HIST_MAP)
            # 适配器补的 trade_date 列 -> 交易日期主键
            m["交易日期"] = "trade_date"
            return m

        # 基础信息：适配器输出中文键名
        if dataset_name == "股票基础信息":
            return {
                "代码": "ts_code", "当前价格": "当前价格",
                "总市值": "总市值", "总市值 (万元)": "总市值 (万元)",
                "总股本": "总股本",
                "名称": "名称", "行业": "行业", "上市日期": "上市时间",
            }
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
        # 0) 新浪 openapi（quotes.sina.cn）—— 最快（0.12s）且科目最齐（64 项）。
        #    实测（2026-10）：官方 JSON 接口，非爬网页；
        #    含 OCF/员工/Capex/V1/V5 全部 AA 所需科目（伊利数值与真值一致）。
        #    旧实现走 AkShare 的 stock_financial_report_sina（另一个新浪路径，
        #    字段仅 71 列），已改为本端点优先。
        try:
            rows = self._sina_openapi_cashflow(code, periods, limit)
            if rows and self._cashflow_usable(rows):
                return rows
        except Exception:                             # noqa: BLE001
            pass
        # 1) 同花顺
        try:
            rows = self._ths_cashflow(code, periods, limit)
            if rows and self._cashflow_usable(rows):
                return rows
        except Exception:                             # noqa: BLE001
            pass
        # 2) 东财（含 D&A / 资本开支 / 已付股息）
        try:
            df = ak.stock_cash_flow_sheet_by_report_em(symbol=_to_em(code))
            if df is not None and len(df):
                rows = self._annualize(df.to_dict("records"), AF.PERIOD_COL_EM, periods)
                for r in rows:
                    r["end_date"] = str(r.get(AF.PERIOD_COL_EM, ""))[:10].replace("-", "")
                return rows[:limit]
        except Exception:                             # noqa: BLE001
            pass
        # 3) 新浪（AkShare 旧路径）降级
        rows = self._sina_report(code, "cashflow", "现金流量表", AF.PERIOD_COL_SINA, periods, limit)
        return rows

    @staticmethod
    def _sina_openapi_report(code: str, report_type: str, num: int = 8) -> list[dict]:
        """新浪官方 JSON 接口取财报三表（非爬网页）。

        端点：https://quotes.sina.cn/cn/api/openapi.php/
                 CompanyFinanceService.getFinanceReport2022
        report_type: lrb(利润表) / fzb(资产负债表) / llb(现金流量表)

        返回：按报告期【升序】的记录列表，每条含「报表日期」及科目中文名。
        """
        import requests
        prefix = "sh" if str(code)[:1] in ("6", "9") else "sz"
        sym = _strip_prefix(code)
        url = ("https://quotes.sina.cn/cn/api/openapi.php/"
               "CompanyFinanceService.getFinanceReport2022")
        r = requests.get(
            url,
            params={"paperCode": f"{prefix}{sym}", "source": report_type,
                    "type": "0", "page": "1", "num": str(num)},
            headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0.0.0 Safari/537.36")},
            timeout=20,
        )
        j = r.json() or {}
        rl = ((j.get("result") or {}).get("data") or {}).get("report_list") or {}
        rows = []
        for period in sorted(rl.keys()):              # 升序
            obj = rl[period] or {}
            rec: dict = {"报表日期": str(period),
                         "end_date": str(period)}
            for it in (obj.get("data") or []):
                title = (it or {}).get("item_title", "")
                val = (it or {}).get("item_value")
                if not title or val in (None, "", "--"):
                    continue
                rec[title] = val
            rows.append(rec)
        return rows

    @classmethod
    def _sina_openapi_cashflow(cls, code: str, periods, limit: int) -> list[dict]:
        """新浪 openapi 现金流量表 -> Tushare 风格行（列名保持中文，与映射层一致）。"""
        rows = cls._sina_openapi_report(code, "llb", num=max(8, (limit or 8)))
        if not rows:
            return []
        # only 年报（12-31）；periods 限定则再过滤（与东财/同花顺路径口径一致）
        wanted = None
        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
        out = []
        for r in rows:
            ed = str(r.get("end_date") or "")
            if not ed.endswith("1231"):
                continue
            if wanted is not None and ed not in wanted:
                continue
            out.append({
                "报表日期": ed,
                "经营活动产生的现金流量净额": r.get("经营活动产生的现金流量净额"),
                "支付给职工以及为职工支付的现金": r.get("支付给职工以及为职工支付的现金"),
                "购建固定资产、无形资产和其他长期资产所支付的现金":
                    r.get("购建固定资产、无形资产和其他长期资产所支付的现金"),
                "处置固定资产、无形资产和其他长期资产所收回的现金净额":
                    r.get("处置固定资产、无形资产和其他长期资产所收回的现金净额"),
                "取得投资收益收到的现金": r.get("取得投资收益收到的现金"),
                "分配股利、利润或偿付利息所支付的现金":
                    r.get("分配股利、利润或偿付利息所支付的现金"),
                "投资活动产生的现金流量净额": r.get("投资活动产生的现金流量净额"),
                "支付的其他与经营活动有关的现金":
                    r.get("支付的其他与经营活动有关的现金"),
                "end_date": ed,
            })
        out.sort(key=lambda x: x["end_date"], reverse=True)
        return out[:limit]

    @staticmethod
    def _cashflow_usable(rows: list[dict]) -> bool:
        """校验同花顺现金流结果是否可用（防脏数据）。

        同花顺风控可能返回「有行但值为空」的结果 —— 若直接采用，
        AA 会在 OCF/资本开支缺失下静默算错。因此要求：
          最近一期同时有【经营活动现金流净额】与【资本开支】。
        """
        for r in rows[:2]:
            ocf = r.get("经营活动产生的现金流量净额")
            capex = r.get("购建固定资产、无形资产和其他长期资产所支付的现金")
            if ocf is not None and capex is not None:
                return True
        return False

    @staticmethod
    def _ths_cashflow(code: str, periods, limit: int) -> list[dict]:
        """同花顺按年度现金流量表 -> Tushare 风格宽表行。

        长表字段（metric_name）经实测验证：
          act_cash_inflow_total / act_cash_outflow_total  -> OCF（差额）
          pay_fixed_assets_etc_cash                       -> 资本开支
          employees_cash_payments                         -> 支付给职工现金
          dispose_fixed_intangible_long_term_assets_loss  -> 处置固定资产收回
          received_invest_income_cash                     -> 取得投资收益收到

        report_period 格式 2024-4 表示年报（-1/-2/-3 为季/中/三季）。
        返回列名保持中文（与新浪后端一致），映射层才能认得。
        """
        import akshare as ak
        sym = _strip_prefix(code)
        df = ak.stock_financial_cash_new_ths(symbol=sym, indicator="按年度")
        if df is None or not len(df):
            return []

        wide = df.pivot_table(index="report_period", columns="metric_name",
                              values="value", aggfunc="first")
        wide = wide.reset_index()

        def _num(v):
            try:
                f = float(v)
                return f if f == f else None
            except (TypeError, ValueError):
                return None

        def _get(r, *names):
            for n in names:
                v = _num(r.get(n))
                if v is not None:
                    return v
            return None

        out: list[dict] = []
        for _, r in wide.iterrows():
            rp = str(r.get("report_period", ""))
            # 只留年报（2024-4）；periods 限定则再过滤
            if not rp.endswith("-4"):
                continue
            yr = rp.split("-")[0]
            end_date = f"{yr}1231"
            if periods:
                wanted = {str(p).replace("-", "")[:8] for p in periods}
                if end_date not in wanted:
                    continue

            inflow = _get(r, "act_cash_inflow_total")
            outflow = _get(r, "act_cash_outflow_total")
            ocf = (inflow - outflow) if (inflow is not None and outflow is not None) else None

            out.append({
                "报表日期": end_date,
                "经营活动产生的现金流量净额": ocf,
                "支付给职工以及为职工支付的现金": _get(r, "employees_cash_payments"),
                "购建固定资产、无形资产和其他长期资产所支付的现金":
                    _get(r, "pay_fixed_assets_etc_cash"),
                "处置固定资产、无形资产和其他长期资产所收回的现金净额":
                    _get(r, "dispose_fixed_intangible_long_term_assets_loss"),
                "取得投资收益收到的现金": _get(r, "received_invest_income_cash"),
                "投资活动产生的现金流量净额":
                    _get(r, "invest_cash_inflow_total", "invest_cash_outflow_total"),
                "分配股利、利润或偿付利息所支付的现金":
                    _get(r, "pay_debt_cash"),
                "end_date": end_date,
            })
        out.sort(key=lambda x: x["end_date"], reverse=True)
        return out[:limit]

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
        # 财年推算：年中（1-6 月）公告的分红属【上一年度】，下半年（7-12 月）属【本年度】
        # 实测伊利：2024-05-29 公告 = 2023 年度分红；2025-12-09 公告 = 2025 中期
        for r in rows:
            ann = str(r.get("公告日期", ""))[:10]
            m = re.match(r"(\d{4})-(\d{2})", ann)
            if m:
                y, mo = int(m.group(1)), int(m.group(2))
                fy = y - 1 if mo <= 6 else y
                r["__report_year__"] = f"{fy}1231"
            else:
                r["__report_year__"] = ""
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

    # -- 财务指标（比率类，不可累加）------------------------------------
    def _fetch_fina_indicator(self, code, periods, limit, **kw) -> list[dict]:
        """zzshare finance_indicator。

        ⚠️ 实测（2026-10）：zzshare indicator 返回【单季度值】，且字段语义不同：
          - 累加型（流量类）：roe / eps / operating_profit → 可按年求和
          - 比率型（不可累加）：gross_profit_margin / net_profit_margin
                                 → 取【年报期值】或年度【均值】
        实测伊利 2024：毛利率单季 36.02/33.84/35.05/31.00，年报口径约 33.98
                       累加会得 135.91（错）；均值得 33.98（对）
        """
        zapi = self._get_api()
        start = "2015-01-01"
        end = "2030-12-31"
        if periods:
            ps = sorted(str(p).replace("-", "") for p in periods)
            start = f"{ps[0][:4]}-01-01"
            end = f"{ps[-1][:4]}-12-31"
        try:
            df = zapi.finance_stock(table="indicator", code=code,
                                    start_date=start, end_date=end)
        except Exception as e:                        # noqa: BLE001
            raise DataSourceError(f"zzshare finance_indicator 失败：{str(e)[:150]}") from e
        if df is None or len(df) == 0:
            return []

        rows = df.to_dict("records")
        # 比率型字段：取年度均值（Q1-Q4 平均）
        RATIO_KEYS = {"gross_profit_margin", "net_profit_margin"}
        # 累加型字段：按年求和
        SUM_KEYS = {"roe", "eps", "operating_profit", "adjusted_profit"}

        buckets: dict[str, dict] = {}
        for r in rows:
            sd = str(r.get("statDate", ""))
            if len(sd) < 10:
                continue
            y = sd[:4]
            b = buckets.setdefault(y, {"statDate": f"{y}-12-31", "_n": 0})
            b["_n"] += 1
            for k, v in r.items():
                if k in ("statDate", "pubDate", "code"):
                    continue
                if not isinstance(v, (int, float)) or v != v:
                    continue
                if k in RATIO_KEYS:
                    b.setdefault("_ratio_sum", {}).setdefault(k, []).append(float(v))
                elif k in SUM_KEYS:
                    b[k] = (b.get(k) or 0.0) + float(v)
                else:
                    b.setdefault(k, float(v))

        out = []
        for y in sorted(buckets, reverse=True):
            b = buckets.pop(y)
            n = b.pop("_n", 1) or 1
            rs = b.pop("_ratio_sum", {})
            for k, vals in rs.items():
                b[k] = sum(vals) / len(vals) if vals else None
            out.append({"end_date": f"{y}1231", "statDate": f"{y}-12-31", **b})

        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
            picked = [r for r in out if r["end_date"] in wanted]
            return picked or out
        return out[:limit]

    # -- 财务指标（年报口径，比率不累加）--------------------------------
    def _fetch_fina_indicator(self, code, periods, limit, **kw) -> list[dict]:
        """AkShare 财务指标（新浪源，已是年报口径）。

        列：报告日 净资产收益率 销售毛利率 销售净利率 每股净资产 资产负债率 ...
        实测伊利 2024：毛利率 ≈ 33.98（年报口径，非单季累加）
        """
        ak = self._get_ak()
        num = _strip_prefix(code)
        rows: list[dict] = []
        try:
            df = ak.stock_financial_analysis_indicator(symbol=num, start_year="2015")
            if df is not None and len(df):
                cols = list(df.columns)
                def pick(*names):
                    for n in names:
                        for c in cols:
                            if n in str(c):
                                return c
                    return None
                c_roe = pick("净资产收益率")
                c_gm = pick("销售毛利率")
                c_nm = pick("销售净利率")
                c_bps = pick("每股净资产")
                c_dar = pick("资产负债率")
                for r in df.to_dict("records"):
                    d = str(r.get("日期", ""))[:10]
                    if not d:
                        continue
                    rows.append({
                        "end_date": d.replace("-", ""),
                        "roe_waa": r.get(c_roe) if c_roe else None,
                        "grossprofit_margin": r.get(c_gm) if c_gm else None,
                        "netprofit_margin": r.get(c_nm) if c_nm else None,
                        "bps": r.get(c_bps) if c_bps else None,
                        "debt_to_assets": r.get(c_dar) if c_dar else None,
                    })
        except Exception:                             # noqa: BLE001
            rows = []

        if not rows:
            raise DataSourceError("akshare 财务指标（新浪）无数据")
        # 只保留年报
        rows = [r for r in rows if r["end_date"][4:8] == "1231"]

        # 毛利率缺失（新浪源近年为 NaN）-> 从利润表自算
        def _is_missing(v):
            return v is None or (isinstance(v, float) and v != v)

        need_gm = [r for r in rows if _is_missing(r.get("grossprofit_margin"))]
        if need_gm:
            try:
                inc = ak.stock_financial_report_sina(stock=_to_sina(code), symbol="利润表")
                if inc is not None and len(inc):
                    gm_map = {}
                    for r in inc.to_dict("records"):
                        d = str(r.get("报告日", ""))[:8]
                        if not d.endswith("1231"):
                            continue
                        rev = r.get("营业总收入") or r.get("营业收入")
                        cost = r.get("营业成本")
                        try:
                            rev, cost = float(rev), float(cost)
                            if rev:
                                gm_map[d[:4]] = (rev - cost) / rev * 100
                        except (TypeError, ValueError):
                            pass
                    for r in rows:
                        y = r["end_date"][:4]
                        if _is_missing(r.get("grossprofit_margin")) and y in gm_map:
                            r["grossprofit_margin"] = gm_map[y]
            except Exception:                         # noqa: BLE001
                pass

        # 按期倒序（最新在前）
        rows = sorted(rows, key=lambda r: r["end_date"], reverse=True)
        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
            picked = [r for r in rows if r["end_date"] in wanted]
            return picked or rows
        return rows[:limit]

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
        """股票基础信息 + 估值快照。

        push2.eastmoney.com 被 IP 风控，多路径降级：
          1) stock_individual_info_em（东财，可能失败）
          2) stock_individual_basic_info_xq（雪球）
          3) 从行情+财报表本地拼凑（始终可用）
        """
        ak = self._get_ak()
        num = _strip_prefix(code)
        out: dict = {"ts_code": code, "symbol": num}

        # 路径 1/2：个股信息接口
        for fn, arg in [("stock_individual_info_em", {"symbol": num}),
                        ("stock_individual_basic_info_xq", {"symbol": _to_em(code)})]:
            f = getattr(ak, fn, None)
            if f is None:
                continue
            try:
                df = f(**arg)
                if df is not None and len(df):
                    for r in df.to_dict("records"):
                        k = r.get("item") or r.get("key")
                        v = r.get("value") or r.get("val")
                        if k and v is not None:
                            out[str(k)] = v
                    if out.get("名称") or out.get("股票简称"):
                        break
            except Exception:                       # noqa: BLE001
                continue

        # 路径 3：本地拼凑（行情 + 财报表）
        if not out.get("名称"):
            try:
                hist = ak.stock_zh_a_hist_tx(symbol=_to_sina(code), adjust="")
                if hist is not None and len(hist):
                    out["当前价格"] = float(hist.iloc[-1]["close"])
                    out["trade_date"] = str(hist.iloc[-1]["date"]).replace("-", "")
            except Exception:                       # noqa: BLE001
                pass
            # 总股本 / 市值（从资产负债表 + 收盘价）
            try:
                bs = ak.stock_financial_report_sina(stock=_to_sina(code), symbol="资产负债表")
                if bs is not None and len(bs):
                    col = "实收资本(或股本)"
                    if col in bs.columns and bs.iloc[0][col] == bs.iloc[0][col]:
                        shares = float(bs.iloc[0][col])           # 股
                        out["总股本"] = shares / 1e4               # -> 万股
                        if out.get("当前价格"):
                            mv_yuan = out["当前价格"] * shares     # 元
                            out["总市值"] = mv_yuan / 1e4          # -> 万元
                            out["总市值 (万元)"] = mv_yuan / 1e4    # 兼容键名
            except Exception:                       # noqa: BLE001
                pass

        out["end_date"] = ""
        return [out] if len(out) > 3 else []

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
