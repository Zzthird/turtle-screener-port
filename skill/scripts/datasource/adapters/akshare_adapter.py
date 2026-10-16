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

import pandas as pd

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


def _rotated(pool: list, idx: int) -> list:
    """把源池从 idx 处循环重排（轮换调度）。

    为何需要：固定降级顺位会让主源承担全部请求（如 333 只全走新浪），
    单源压力集中且易触发风控。轮换后每源只需承担 1/N。

    例（pool=[A,B,C,D,E,F]）：
      idx=0 → [A,B,C,D,E,F]
      idx=1 → [B,C,D,E,F,A]
      idx=6 → [A,B,C,D,E,F]
    重排后仍按顺序尝试，失败自动降级 —— 轮换与降级兼得。
    """
    if not pool:
        return pool
    s = int(idx) % len(pool)
    return list(pool[s:]) + list(pool[:s])


def _num_or_none(v):
    """把源返回的字符串/空值统一转成 float 或 None。

    同花顺（AkShare 封装）的 value 列是【字符串】（如 "115393310976.6900"），
    需转 float；"--"/""/None/NaN 一律当缺失。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)          # NaN -> None
    s = str(v).strip().replace(",", "")
    if s in ("", "--", "-", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


@register
class AkshareAdapter(DataSource):
    name = "akshare"

    # ── 轮换调度：当前股票序号 ────────────────────────────
    # 由 screener 在 Tier2 主循环中 set()，各 _fetch_* 读取后决定
    # 源池从哪个源开始。用 contextvars 而非参数透传：
    # ctx -> _free_call -> dataset_call -> get -> adapter 链路长，
    # 逐层加参数要改 6 处签名，contextvar 只需改 2 处。
    _CUR_IDX: "contextvars.ContextVar[int]" = None    # 在 __init__ 后设置

    @classmethod
    def set_rotation_index(cls, idx: int) -> None:
        """由 screener 调用：设置当前股票的轮换序号。"""
        import contextvars
        if cls._CUR_IDX is None:
            cls._CUR_IDX = contextvars.ContextVar("akshare_rot_idx", default=0)
        cls._CUR_IDX.set(int(idx))

    @classmethod
    def _rot_idx(cls) -> int:
        import contextvars
        if cls._CUR_IDX is None:
            cls._CUR_IDX = contextvars.ContextVar("akshare_rot_idx", default=0)
        return int(cls._CUR_IDX.get() or 0)
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
    # ── 轮换池：每表一组可轮换的源 ────────────────────
    # 每个 _fetch_* 按 _rot_idx() 从不同源开始，失败自动降级。
    # 首次全量跑：每源承担 ~333/N 次；重跑全命中缓存 → 0 请求。
    # 源池顺序 = 降级优先级（越靠前越快/越稳）。
    #
    # ⚠️ 为何排除两个源：
    #   1) 东财（em）—— 已作为 _src_* 实现但【未入池】：
    #      a. 慢，2-4s/表（其他源 0.1-0.3s）
    #      b. field_overrides 里【只有现金流量表有东财映射表】
    #         （CASHFLOW_EM_MAP），利润表/资产负债表缺 INCOME_EM_MAP /
    #         BALANCE_EM_MAP → 列名无法回译成中文业务名 →
    #         实测茅台走东财后 revenue 全为 None → AA 算不出（years=0）。
    #   2) 智兔（zhitu）—— 额度极小（当日数十次即 429），
    #      且三表字段有缺（所得税费用/应付账款），单独跑会降级到
    #      「§17.5(OCF护)」口径（五粮液 AA 35,262 → 31,823，偏差 9.8%）。
    #    两者仍保留在 _src_* 中，可手动指定 source= 使用。
    _INCOME_SRCS = ("sina_openapi", "ths_new", "xq", "sina_ak")
    _BALANCE_SRCS = ("sina_openapi", "ths_new", "xq", "sina_ak")
    _CASHFLOW_SRCS = ("sina_openapi", "ths_new", "xq", "sina_ak")

    def _src_income(self, name, code, periods, limit):
        ak = self._get_ak()
        if name == "sina_openapi":
            return self._sina_openapi_income(code, periods, limit)
        if name == "ths_new":
            return self._ths_new_report(code, "benefit", periods, limit)
        if name == "em":
            df = ak.stock_profit_sheet_by_report_em(symbol=_to_em(code))
            if df is None or not len(df):
                return []
            rows = self._annualize(df.to_dict("records"), AF.PERIOD_COL_EM, periods)
            for r in rows:
                r["end_date"] = str(r.get(AF.PERIOD_COL_EM, ""))[:10].replace("-", "")
            return rows[:limit]
        if name == "xq":
            return self._xueqiu_report(code, "income", periods, limit)
        if name == "zhitu":
            return self._zhitu_report(code, "income", periods, limit)
        if name == "sina_ak":
            return self._sina_report(code, "income", "利润表", AF.PERIOD_COL_SINA,
                                     periods, limit)
        return []

    def _src_balance(self, name, code, periods, limit):
        ak = self._get_ak()
        if name == "sina_openapi":
            return self._sina_openapi_balance(code, periods, limit)
        if name == "ths_new":
            return self._ths_new_report(code, "debt", periods, limit)
        if name == "em":
            df = ak.stock_balance_sheet_by_report_em(symbol=_to_em(code))
            if df is None or not len(df):
                return []
            rows = self._annualize(df.to_dict("records"), AF.PERIOD_COL_EM, periods)
            for r in rows:
                r["end_date"] = str(r.get(AF.PERIOD_COL_EM, ""))[:10].replace("-", "")
            return rows[:limit]
        if name == "xq":
            return self._xueqiu_report(code, "balance", periods, limit)
        if name == "zhitu":
            return self._zhitu_report(code, "balance", periods, limit)
        if name == "sina_ak":
            return self._sina_report(code, "balancesheet", "资产负债表",
                                     AF.PERIOD_COL_SINA, periods, limit)
        return []

    def _src_cashflow(self, name, code, periods, limit):
        ak = self._get_ak()
        if name == "sina_openapi":
            return self._sina_openapi_cashflow(code, periods, limit)
        if name == "ths_new":
            return self._ths_cashflow(code, periods, limit)
        if name == "em":
            df = ak.stock_cash_flow_sheet_by_report_em(symbol=_to_em(code))
            if df is None or not len(df):
                return []
            rows = self._annualize(df.to_dict("records"), AF.PERIOD_COL_EM, periods)
            for r in rows:
                r["end_date"] = str(r.get(AF.PERIOD_COL_EM, ""))[:10].replace("-", "")
            return rows[:limit]
        if name == "xq":
            return self._xueqiu_report(code, "cash_flow", periods, limit)
        if name == "zhitu":
            return self._zhitu_report(code, "cashflow", periods, limit)
        if name == "sina_ak":
            return self._sina_report(code, "cashflow", "现金流量表",
                                     AF.PERIOD_COL_SINA, periods, limit)
        return []

    def _fetch_income(self, code, periods, limit, **kw):
        """利润表：6 源轮换 + 逐源降级。"""
        order = _rotated(list(self._INCOME_SRCS), self._rot_idx())
        for name in order:
            try:
                rows = self._src_income(name, code, periods, limit)
                if rows:
                    return rows
            except Exception:                         # noqa: BLE001
                continue
        return []

    def _fetch_balancesheet(self, code, periods, limit, **kw):
        """资产负债表：6 源轮换 + 逐源降级。"""
        order = _rotated(list(self._BALANCE_SRCS), self._rot_idx())
        for name in order:
            try:
                rows = self._src_balance(name, code, periods, limit)
                if rows:
                    return rows
            except Exception:                         # noqa: BLE001
                continue
        return []

    # -- 同花顺（AkShare 封装）利润表 / 资产负债表 ------------------
    # 同花顺是【长表】（metric_name / value），需 pivot 成宽表。
    # 数值均为【字符串】，需转 float。
    # 字段名为同花顺自造英文；已验证 2024 伊利：
    #   营业总收入 1153.93亿 / 归母净利 84.53亿 / 总资产 1537.18亿 /
    #   总负债 967.10亿（total_debt）/ 负债率 62.91% —— 与东财一致。
    _THS_METRIC_MAP = {
        # 利润表
        "benefit": {
            # ⚠️ 同花顺这两个字段的含义与字面相反（实测茅台/工行/宁德均如此）：
            #    operating_income       = 营业收入（主营，不含利息/保费等）
            #    operating_income_total = 营业总收入（含利息收入等）
            #  原实现写反，导致 AA 的 S（真实现金收入的起点）取错：
            #  茅台 2024 总收 1741.44 亿 / 营收 1708.99 亿，取错则 AA 偏高 3.07%。
            "营业总收入": "operating_income_total",
            "营业收入": "operating_income",
            "营业成本": "operating_costs",
            "营业税金及附加": "taxes_and_surcharges",
            "销售费用": "sales_fee",
            "管理费用": "manage_fee",
            "研发费用": "research_and_development_expenses",
            # ⚠️ 同花顺【没有】单独的「财务费用」字段。
            #    financial_interest_expenses 只是【利息支出】（借钱利息），
            #    漏掉利息收入（存款利息）——对现金充裕的公司影响巨大：
            #    五粮液 2025 利息支出 0.29 亿 vs 利息收入 26.93 亿，
            #    财务费用真值 = -26.65 亿（新浪）。若直接用
            #    financial_interest_expenses 会得到 +0.29 亿 → 符号都反了
            #    → AA 的 W4 凭空多记一笔巨额支出。
            #    故此处【不映射】，改在 _ths_new_report 里做差值。
            "投资收益": "invest_income",
            "营业利润": "operating_profit",
            "利润总额": "profit_total",
            "所得税费用": "income_tax_expense",
            "净利润": "net_profit",
            "归属于母公司所有者的净利润": "parent_holder_net_profit",
            "营业外收入": "non_operating_income",
            "营业外支出": "non_operating_expenses",
            "基本每股收益": "basic_eps",
        },
        # 资产负债表
        "debt": {
            "货币资金": "cash",
            "交易性金融资产": "trading_financial_assets",
            "应收账款": "accounts_receivable",
            "应收票据": "notes_receivable",
            "预付款项": "prepayments",
            "存货": "inventory",
            "商誉": "goodwill",
            "资产总计": "assets_total",
            "负债合计": "total_debt",
            "应付账款": "accounts_payable",
            "预收款项": "advance_receipts",
            "合同负债": "contract_debt",
            "流动资产合计": "total_current_assets",
            "固定资产净额": "fixed_assets",
        },
    }

    def _ths_new_report(self, code, table: str, periods, limit: int) -> list[dict]:
        """同花顺（AkShare 封装）利润表/资产负债表 -> 中文列名行。

        table: "benefit"（利润表） | "debt"（资产负债表）
        长表 pivot 成宽表，只保留年报（12-31）。
        """
        ak = self._get_ak()
        fn = (ak.stock_financial_benefit_new_ths if table == "benefit"
              else ak.stock_financial_debt_new_ths)
        sym = _strip_prefix(code)
        df = fn(symbol=sym, indicator="按年度")
        if df is None or len(df) == 0:
            return []
        need = self._THS_METRIC_MAP.get(table) or {}
        # 只保留映射表里认识的 metric（need 是 {中文: 英文}，比对英文那侧）
        known = set(need.values())
        keep = {m for m in df["metric_name"].dropna().unique() if m in known}
        if not keep:
            return []
        sub = df[df["metric_name"].isin(keep)].copy()
        # 财务费用特例：同花顺无此字段，用【利息支出 − 利息收入】合成。
        #   ⚠️ 局限：合成式只含利息，不含汇兑损益/手续费等 ——
        #   对有外汇/衍生工具的公司会偏。实测美的集团 2025：
        #   同花顺合成 -62.33 亿 vs 新浪真值 -59.04 亿（差 3.29 亿）
        #   → AA 偏差 +0.21%（可接受）。
        #   若未来偏差扩大，可考虑仅在无其他源时启用同花顺路径。
        fin_series = None
        if table == "benefit":
            extra = df[df["metric_name"].isin(
                ["financial_interest_expenses", "financial_interest_income"])].copy()
            if len(extra):
                extra["report_date"] = extra["report_date"].astype(str)
                px = extra.pivot_table(index="report_date", columns="metric_name",
                                       values="value", aggfunc="first")
                ie = pd.to_numeric(px.get("financial_interest_expenses"),
                                   errors="coerce").fillna(0.0)
                ii = pd.to_numeric(px.get("financial_interest_income"),
                                   errors="coerce").fillna(0.0)
                fin_series = (ie - ii)
                fin_series.index = fin_series.index.astype(str)
        sub["report_date"] = sub["report_date"].astype(str)
        sub = sub[sub["report_date"].str.endswith("12-31")]
        if sub.empty:
            return []
        wanted = None
        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
        # pivot: index=report_date, columns=metric_name
        piv = sub.pivot_table(index="report_date", columns="metric_name",
                              values="value", aggfunc="first")
        out = []
        for rd, row in piv.sort_index(ascending=False).iterrows():
            ed = rd.replace("-", "")
            if wanted is not None and ed not in wanted:
                continue
            rec: dict = {"报表日期": ed, "end_date": ed}
            for cn, metric in need.items():
                v = row.get(metric)
                rec[cn] = _num_or_none(v)
            # 财务费用：同花顺无此字段，用合成的值补上
            if fin_series is not None:
                rk = rd if rd in fin_series.index else rd.replace("-", "")[:4] + "-12-31"
                try:
                    rec["财务费用"] = _num_or_none(fin_series.get(rk))
                except Exception:                     # noqa: BLE001
                    pass
            out.append(rec)
        return out[:limit]

    # -- 雪球（pysnowball 端点）三表 ---------------------------------
    # ⚠️ 雪球是【宽表】而非新浪式的 item_title 列表：
    #    list[i] = {report_name, report_date, 字段: [值, yoy], ...}
    #    值取 [0]，yoy 取 [1]；无值时为 null。
    #  需 cookie xq_a_token（可用 XQ_TOKEN 环境变量 / config 覆盖）。
    #  实测 0.11s/表（最快），但 Token 会过期 → 仅作轮换位之一。
    _XQ_MAP = {
        "income": {
            "营业总收入": "total_revenue",
            "营业收入": "revenue",
            "营业成本": "operating_cost",
            "营业税金及附加": "operating_taxes_and_surcharge",
            "销售费用": "sales_fee",
            "管理费用": "manage_fee",
            "研发费用": "rad_cost",
            "财务费用": "financing_expenses",
            "投资收益": "invest_income",
            "营业利润": "op",
            "利润总额": "profit_total_amt",
            "所得税费用": "income_tax_expenses",
            "净利润": "net_profit",
            "归属于母公司所有者的净利润": "net_profit_atsopc",
            "营业外收入": "non_operating_income",
            "营业外支出": "non_operating_payout",
            "基本每股收益": "basic_eps",
        },
        "balance": {
            "货币资金": "currency_funds",
            "应收账款": "account_receivable",
            "应付账款": "accounts_payable",
            "预付款项": "pre_payment",
            "存货": "inventory",
            "商誉": "good_will",
            "资产总计": "total_assets",
            "负债合计": "total_liab",
            "合同负债": "contract_liabilities",
            "流动资产合计": "total_current_assets",
            "固定资产净额": "fix_assets",
        },        "cash_flow": {
            "经营活动产生的现金流量净额": "ncf_from_oa",
            # ⚠️ 科目名必须与 CASHFLOW_SINA_MAP 的 value 完全一致（含「所」字）。
            #    雪球/智兔原始返回无「所」字（「...其他长期资产支付的现金」），
            #    直接透传会匹配不上 → Capex 静默为 None → AA 走降级口径。
            "购建固定资产、无形资产和其他长期资产所支付的现金":
                "cash_paid_for_assets",
            "分配股利、利润或偿付利息所支付的现金": "cash_paid_of_distribution",
            "支付给职工以及为职工支付的现金": "cash_paid_to_employee_etc",
            "支付的各项税费": "payments_of_all_taxes",
            "销售商品、提供劳务收到的现金": "cash_received_of_sales_service",
            "购买商品、接受劳务支付的现金": "goods_buy_and_service_cash_pay",
            "投资活动产生的现金流量净额": "ncf_from_ia",
            "取得投资收益收到的现金": "invest_income_cash_received",
            "处置固定资产、无形资产和其他长期资产所收回的现金净额": "net_cash_of_disposal_assets",
        },
    }

    @staticmethod
    def _xq_token() -> str:
        """雪球 token：环境变量 XQ_TOKEN 优先，其次项目 config。"""
        import os
        t = (os.environ.get("XQ_TOKEN") or "").strip()
        if t:
            return t
        try:
            import config                                 # type: ignore
            return str(getattr(config, "XQ_TOKEN", "") or "").strip()
        except Exception:                                 # noqa: BLE001
            return ""

    @classmethod
    def _xueqiu_report(cls, code: str, table: str, periods, limit: int) -> list[dict]:
        """雪球三表 -> 中文列名行（宽表直接用）。

        table: income | balance | cash_flow
        """
        import requests
        token = cls._xq_token()
        if not token:
            return []
        num, mkt = (code.split(".") + ["SH"])[:2] if "." in code else (code, "SH")
        sym = f"{mkt.upper()}{num}"
        url = f"https://stock.xueqiu.com/v5/stock/finance/cn/{table}.json"
        r = requests.get(
            url,
            params={"symbol": sym, "type": "Q4", "is_detail": "true",
                    "count": str(max(5, (limit or 8)))},
            headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0.0.0 Safari/537.36"),
                     "Cookie": f"xq_a_token={token}"},
            timeout=15,
        )
        j = r.json() or {}
        lst = ((j.get("data") or {}).get("list")) or []
        if not lst:
            return []
        need = cls._XQ_MAP.get(table) or {}
        wanted = None
        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
        out = []
        for item in lst:
            # 雪球 report_date 是毫秒时间戳（UTC），转北京时间后才是 12-31；
            # report_name 形如「2024年报」更可靠，优先用它。
            rname = str(item.get("report_name") or "")
            if "年报" in rname:
                rd = "".join(ch for ch in rname if ch.isdigit())[:4] + "1231"
            else:
                raw = item.get("report_date")
                try:
                    import datetime as _d
                    rd = _d.datetime.fromtimestamp(int(raw) / 1000).strftime("%Y%m%d")
                except Exception:                     # noqa: BLE001
                    rd = ""
            if not rd.endswith("1231"):
                continue
            if wanted is not None and rd not in wanted:
                continue
            rec: dict = {"报表日期": rd, "end_date": rd}
            for cn, field in need.items():
                raw = item.get(field)
                if isinstance(raw, (list, tuple)):
                    raw = raw[0] if raw else None
                rec[cn] = _num_or_none(raw)
            out.append(rec)
        out.sort(key=lambda x: x["end_date"], reverse=True)
        return out[:limit]

    # -- 智兔（拼音字段）三表 ---------------------------------------
    # 端点：/hs/fin/{income|balance|cashflow}/{code}?token=xxx
    #   ★ code 在【路径】里，不是 query 参数（写成 query 会 404）。
    # 特点：一次返回【全历史】（1995 起，129-249KB），故本地筛年报后
    #       只取 limit 条；字段为拼音缩写，靠数值对照反推确认。
    # 实测（伊利 2025 年报）全部字段与真值一致。
    _ZT_MAP = {
        "income": {
            "营业总收入": "yyzsr",
            "营业收入": "yysr",
            "营业成本": "yycb",
            "营业税金及附加": "yysjjfj",
            "销售费用": "xsfy",
            "管理费用": "glfy",
            "财务费用": "cwfy",
            "投资收益": "tzsy",
            "营业利润": "yylr",
            "利润总额": "lrze",
            "净利润": "jlr",
            "归属于母公司所有者的净利润": "gsmgsyzzdjlr",
            "营业外收入": "ywsr",
            "营业外支出": "ywzc",
        },
        "balance": {
            "货币资金": "hbzj",
            "应收账款": "yszk",
            "存货": "ch",
            "资产总计": "zczj",
            "负债合计": "fzhj",
            "合同负债": "ysk",
        },
        "cashflow": {
            "经营活动产生的现金流量净额": "jyhdcsdxjlxj",
            # ⚠️ 与 CASHFLOW_SINA_MAP 的 value 对齐（含「所」字）
            "购建固定资产、无形资产和其他长期资产所支付的现金":
                "gjgdzcwxzhqtqctzzfdxj",
            "分配股利、利润或偿付利息所支付的现金": "fpglrlhcllxzfdxj",
            "支付给职工以及为职工支付的现金": "zfgzyjwzgzfdxj",
            "支付的各项税费": "zfdgxsf",
            "销售商品、提供劳务收到的现金": "xssptglwsddxj",
            "购买商品、接受劳务支付的现金": "gmspjslwzfdxj",
            "投资活动产生的现金流量净额": "tzhdcsdxjlxj",
            "处置固定资产、无形资产和其他长期资产所收回的现金净额":
                "czgdzcwxzhqtqctzssddxj",
            "取得投资收益收到的现金": "qdtzsysddxj",
        },
    }

    @classmethod
    def _zhitu_report(cls, code: str, table: str, periods, limit: int) -> list[dict]:
        """智兔三表 -> 中文列名行。

        table: income | balance | cashflow
        """
        import os
        import requests
        tok = (os.environ.get("ZHITU_TOKEN") or "").strip()
        if not tok:
            try:
                import config                             # type: ignore
                tok = str(getattr(config, "ZHITU_TOKEN", "") or "").strip()
            except Exception:                             # noqa: BLE001
                tok = ""
        if not tok:
            return []
        r = requests.get(
            f"https://api.zhituapi.com/hs/fin/{table}/{code}",
            params={"token": tok}, timeout=25,
        )
        data = r.json()
        if not isinstance(data, list) or not data:
            return []
        need = cls._ZT_MAP.get(table) or {}
        wanted = None
        if periods:
            wanted = {str(p).replace("-", "")[:8] for p in periods}
        rows = []
        for d in data:
            rd = str(d.get("jzrq") or "").replace("-", "")[:8]
            if not rd.endswith("1231"):
                continue
            if wanted is not None and rd not in wanted:
                continue
            rec: dict = {"报表日期": rd, "end_date": rd}
            for cn, field in need.items():
                rec[cn] = _num_or_none(d.get(field))
            rows.append(rec)
        rows.sort(key=lambda x: x["end_date"], reverse=True)
        return rows[:limit]

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

    # -- 现金流量表（6 源轮换 + 逐源降级）--------------------------------
    def _fetch_cashflow(self, code, periods, limit, **kw) -> list[dict]:
        """现金流量表：6 源轮换 + 逐源降级，每源后接 _cashflow_usable() 校验。

        ⚠️ 校验必要性：某些源风控时会返回「有行但值为空」的结果，
        若直接采用，AA 会在 OCF/Capex 缺失下静默算错。
        """
        order = _rotated(list(self._CASHFLOW_SRCS), self._rot_idx())
        for name in order:
            try:
                rows = self._src_cashflow(name, code, periods, limit)
                if rows and self._cashflow_usable(rows):
                    return rows
            except Exception:                         # noqa: BLE001
                continue
        return []

    @staticmethod
    def _sina_openapi_report(code: str, report_type: str, num: int = 8) -> list[dict]:
        """新浪官方 JSON 接口取财报三表（非爬网页）。

        端点：https://quotes.sina.cn/cn/api/openapi.php/
                 CompanyFinanceService.getFinanceReport2022
        report_type: lrb(利润表) / fzb(资产负债表) / llb(现金流量表)

        返回：按报告期【升序】的记录列表，每条含「报表日期」及科目中文名。
        """
        import requests
        # 交易所前缀：优先用 ts_code 的后缀（.SH/.SZ/.BJ），最可靠；
        # 无后缀时回退数字前缀（见 a-stock-data #46：51x/588x/900x 也是沪市，
        # 仅用 code[0] in "69" 会漏判）。
        _c = str(code).upper()
        if _c.endswith(".SH"):
            prefix = "sh"
        elif _c.endswith(".SZ"):
            prefix = "sz"
        elif _c.endswith(".BJ"):
            prefix = "bj"
        else:
            _d = "".join(ch for ch in _c if ch.isdigit()).zfill(6)
            prefix = "sh" if _d[:3] in ("900",) or _d[0] in "69" \
                or _d.startswith(("5", "51", "58")) else "sz"
        sym = _strip_prefix(code)
        url = ("https://quotes.sina.cn/cn/api/openapi.php/"
               "CompanyFinanceService.getFinanceReport2022")
        # type 参数是【季度选择器】，不是「按年度/按报告期」：
        #   0=全部（混季报） 1=一季报 2=中报 3=三季报 4=年报
        # 本函数只服务年报口径，故固定 type=4 ——
        # 可直接用 num 拿 N 期【年报】，无需多拉再筛（省 ~75% 流量）。
        r = requests.get(
            url,
            params={"paperCode": f"{prefix}{sym}", "source": report_type,
                    "type": "4", "page": "1", "num": str(num)},
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

    @classmethod
    def _sina_openapi_income(cls, code: str, periods, limit: int) -> list[dict]:
        """新浪 openapi 利润表 -> 中文列名行（与映射层 AKSHARE INCOME_MAP 对齐）。

        实测（600887）：77 个科目，含营业总收入/成本/所得税费用/财务费用/
        投资收益/归属于母公司所有者的净利润 等 AA 所需字段。

        注：输出【中文列名】而非 Tushare 英文名 ——
            field_overrides 会按 dataset_name 选 INCOME_MAP（中文->中文），
            故适配器必须给映射层同样的中文 key。
        """
        return cls._sina_openapi_generic(code, "lrb", periods, limit)

    @classmethod
    def _sina_openapi_balance(cls, code: str, periods, limit: int) -> list[dict]:
        """新浪 openapi 资产负债表 -> 中文列名行（与 BALANCE_MAP 对齐）。"""
        return cls._sina_openapi_generic(code, "fzb", periods, limit)

    @classmethod
    def _sina_openapi_generic(cls, code: str, report_type: str, periods,
                              limit: int) -> list[dict]:
        """新浪 openapi 通用取表：拉全科目后只保留年报（12-31）。

        输出【中文科目名】——映射层（akshare_fields.INCOME_MAP/BALANCE_MAP）
        的 key 也是中文，故直接透传即可；不需做英文回译。
        """
        rows = cls._sina_openapi_report(code, report_type,
                                        num=max(8, (limit or 8)))
        if not rows:
            return []
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
            rec = dict(r)                            # 已含中文科目 + end_date
            rec["报表日期"] = ed
            out.append(rec)
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
                # ⚠️ 同花顺（AkShare 封装）现金流量表【没有】「处置固定资产收回」
                #    这一现金口径科目。原实现在这里填了
                #      dispose_fixed_intangible_long_term_assets_loss
                #    但那是【利润表的处置损失】（五粮液 2025 = -1269 万），
                #    与「处置收到的现金」（新浪 +0.37 亿）概念完全不同，
                #    符号也常相反 → 会污染 AA 的 V1 项。故留空。
                #    （同花顺另有 fixed_assets_net_cash，但口径不明，不采用。）
                "取得投资收益收到的现金": _get(r, "received_invest_income_cash"),
                # 投资活动净额：同花顺有专门的 invest_cash_flow_net，
                # 不要用 inflow_total/outflow_total 相减（会得到毛额而非净额）
                "投资活动产生的现金流量净额": _get(r, "invest_cash_flow_net"),
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
