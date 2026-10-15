"""统一数据层 schema — 按业务语义定义，不按 API 字段名定义。

设计原则：
1. 字段名用**业务语义**（中文），不用 Tushare 的英文缩写 → 换源时映射表是纯配置
2. 每个值携带**元信息**（单位/币种/期间）→ 防币种与单位静默错误
3. 字段清单由 `derived_metrics.py` / `valuation_engine.py` 的**实际引用**倒推
   → 不多也不少

变更记录：
- 字段来源：扫描 tushare_modules/*.py + quality_control.py + valuation_engine.py
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------

SourceName = Literal["tushare", "zzshare", "akshare", "yfinance", "manual"]
ReportType = Literal["合并", "母公司"]
Unit = Literal["元", "百万元", "亿元", "股", "手", "元/股", "pct"]

UNIT_BASE: Unit = "百万元"  # 框架全文统一：百万元


@dataclass(frozen=True)
class Value:
    """带元信息的数值。所有数据层的值都必须是 Value，禁止裸数。

    为什么必须带元信息：框架踩过的真实坑——
      - 人民币利润 ÷ 港元市值 → 5-10% 偏差
      - 不同源单位不同（元/万元/百万元）→ 静默算错
    """

    value: Optional[float]
    unit: Unit = UNIT_BASE
    currency: str = "CNY"
    period: Optional[str] = None          # FY2024 / 2025Q3 / 2025H1
    report_type: Optional[ReportType] = None
    source: Optional[SourceName] = None
    fetched_at: Optional[str] = None
    note: Optional[str] = None            # 缺失原因等
    text: Optional[str] = None            # 文本类字段的值（股东名/进度等）

    @property
    def missing(self) -> bool:
        return self.value is None and self.text is None

    def to_yi(self) -> Optional[float]:
        """转亿元（正文展示口径）。"""
        if self.value is None:
            return None
        if self.unit == "百万元":
            return self.value / 100
        if self.unit == "元":
            return self.value / 1e8
        if self.unit == "亿元":
            return self.value
        return None

    def to_mm(self) -> Optional[float]:
        """转百万元（计算口径）。"""
        if self.value is None:
            return None
        if self.unit == "亿元":
            return self.value * 100
        if self.unit == "元":
            return self.value / 1e6
        if self.unit == "百万元":
            return self.value
        return None

    def __repr__(self) -> str:
        if self.value is None and self.text is None:
            return f"Value(MISSING, note={self.note!r})"
        if self.value is None:
            return f"Value({self.text!r})"
        return f"Value({self.value:,.2f} {self.unit} {self.currency})"


# ---------------------------------------------------------------------------
# 字段定义（按业务语义，由实际引用倒推）
# ---------------------------------------------------------------------------

@dataclass
class FieldSpec:
    """单个业务字段的定义。

    `tushare`/`zzshare`/`akshare` 为该源的原生字段名；None 表示该源无此字段。
    同名时（如 zzshare 兼容 Tushare）可只写 `tushare`，映射时自动回退。
    """

    key: str                    # 业务语义名（中文）
    unit: Unit = UNIT_BASE
    desc: str = ""
    required: bool = False      # 框架计算是否必需（缺失则降级）
    tushare: Optional[str] = None
    zzshare: Optional[str] = None
    akshare: Optional[str] = None
    kind: str = "num"           # "num" 数值 | "str" 文本 | "date" 日期

    def native(self, source: str) -> Optional[str]:
        """返回该源的原生字段名。zzshare 兼容 Tushare，未显式指定时回退。"""
        explicit = getattr(self, source, None)
        if explicit:
            return explicit
        if source == "zzshare":
            return self.tushare          # zzshare 字段名兼容 Tushare
        return None


# ============ 利润表 (income) ============
# zzshare 实测字段名来自 adapters/zzshare_fields.py（与 Tushare 完全不同）
INCOME_FIELDS: list[FieldSpec] = [
    FieldSpec("营业收入", "元", "revenue", True, "revenue", "operating_revenue", "营业收入"),
    FieldSpec("营业成本", "元", "oper_cost", True, "oper_cost", "operating_cost", "营业成本"),
    FieldSpec("研发费用", "元", "rd_exp", False, "rd_exp", "research_expense", "研发费用"),
    FieldSpec("销售费用", "元", "sell_exp", False, "sell_exp", "sale_expense", "销售费用"),
    FieldSpec("管理费用", "元", "admin_exp", False, "admin_exp", "management_expense", "管理费用"),
    FieldSpec("财务费用", "元", "finance_exp", False, "finance_exp", "financial_expense", "财务费用"),
    FieldSpec("经营利润", "元", "operate_profit", False, "operate_profit", "operating_profit", "营业利润"),
    FieldSpec("利润总额", "元", "total_profit", False, "total_profit", "total_profit", "利润总额"),
    FieldSpec("所得税", "元", "income_tax", True, "income_tax", "income_tax", "所得税费用"),
    FieldSpec("净利润", "元", "n_income", False, "n_income", "net_profit", "净利润"),
    FieldSpec("归母净利润", "元", "n_income_attr_p", True, "n_income_attr_p", "np_parent_company_owners", "归属于母公司所有者的净利润"),
    FieldSpec("少数股东损益", "元", "minority_gain", True, "minority_gain", "minority_interest", "少数股东损益"),
    FieldSpec("扣非归母净利润", "元", "profit_dedt", False, "profit_dedt", "deduct_parent_net_profit", None),
    FieldSpec("基本EPS", "元/股", "basic_eps", False, "basic_eps", "eps", "基本每股收益"),
]

# ============ 资产负债表 (balancesheet) ============
BALANCE_FIELDS: list[FieldSpec] = [
    FieldSpec("货币资金", "元", "money_cap", True, "money_cap", "cash_equivalents", "货币资金"),
    FieldSpec("交易性金融资产", "元", "trad_asset", False, "trad_asset", "trading_financial_assets", "交易性金融资产"),
    FieldSpec("应收账款", "元", "accounts_receiv", True, "accounts_receiv", "accounts_receivable", "应收账款"),
    FieldSpec("存货", "元", "inventories", False, "inventories", "inventory", "存货"),
    FieldSpec("合同负债", "元", "contract_liab", True, "contract_liab", "contract_liabilities", "合同负债"),
    FieldSpec("应付账款", "元", "acct_payable", True, "acct_payable", "accounts_payable", "应付账款"),
    FieldSpec("商誉", "元", "goodwill", False, "goodwill", "goodwill", "商誉"),
    FieldSpec("递延所得税资产", "元", "defer_tax_assets", False, "defer_tax_assets", "deferred_tax_assets", "递延所得税资产"),
    FieldSpec("递延所得税负债", "元", "defer_tax_liab", False, "defer_tax_liab", "deferred_tax_liabilities", "递延所得税负债"),
    FieldSpec("总资产", "元", "total_assets", True, "total_assets", "total_assets", "资产总计"),
    FieldSpec("总负债", "元", "total_liab", True, "total_liab", "total_liability", "负债合计"),
    FieldSpec("归母权益", "元", "total_hldr_eqy_exc_min_int", True, "total_hldr_eqy_exc_min_int", "owners_equity_including_minority_interest", "归属于母公司股东权益合计"),
    FieldSpec("总股本", "股", "total_share", True, "total_share", "capitalization", "实收资本(或股本)"),
]

# ============ 现金流量表 (cashflow) ============
CASHFLOW_FIELDS: list[FieldSpec] = [
    FieldSpec("经营活动现金流", "元", "n_cashflow_act", True, "n_cashflow_act", "net_operate_cash_flow", "经营活动产生的现金流量净额"),
    FieldSpec("资本开支", "元", "c_pay_acq_const_fiolta", True, "c_pay_acq_const_fiolta", "buy_construct_long_term_assets", "购建固定资产、无形资产和其他长期资产所支付的现金"),
    FieldSpec("折旧摊销_固定资产", "元", "depr_fa_coga_dpba", True, "depr_fa_coga_dpba", "depr_fa_coga_dpba", None),
    FieldSpec("摊销_无形资产", "元", "amort_intang_assets", True, "amort_intang_assets", "amort_intang_assets", None),
    FieldSpec("摊销_长期待摊", "元", "lt_amort_deferred_exp", True, "lt_amort_deferred_exp", "lt_amort_deferred_exp", None),
    FieldSpec("已付股息", "元", "c_pay_dist_dpcp_int_exp", True, "c_pay_dist_dpcp_int_exp", "cash_dividend_internal", "分配股利、利润或偿付利息所支付的现金"),
    FieldSpec("支付给职工现金", "元", "c_pay_to_staff", False, "c_pay_to_staff", None, "支付给职工以及为职工支付的现金"),
]

# ============ 财务指标 (fina_indicator) ============
INDICATOR_FIELDS: list[FieldSpec] = [
    FieldSpec("加权ROE", "pct", "roe_waa", True, "roe_waa", "roe", None),
    FieldSpec("毛利率", "pct", "grossprofit_margin", False, "grossprofit_margin", "gross_profit_margin", None),
    FieldSpec("净利率", "pct", "netprofit_margin", False, "netprofit_margin", "net_profit_margin", None),
    FieldSpec("每股净资产", "元/股", "bps", False, "bps", None, None),
    FieldSpec("资产负债率", "pct", "debt_to_assets", False, "debt_to_assets", None, None),
]

# ============ 每日指标 (daily_basic) ============
DAILY_BASIC_FIELDS: list[FieldSpec] = [
    FieldSpec("收盘价", "元", "close", True, "close", None, None),
    FieldSpec("总市值", "元", "total_mv", True, "total_mv", "market_cap", None),
    FieldSpec("流通市值", "元", "circ_mv", False, "circ_mv", "circulating_market_cap", None),
    FieldSpec("PE_TTM", "pct", "pe_ttm", True, "pe_ttm", "pe_ratio", None),
    FieldSpec("PB", "pct", "pb", False, "pb", "pb_ratio", None),
    FieldSpec("股息率TTM", "pct", "dv_ttm", False, "dv_ttm", None, None),
    FieldSpec("总股本", "股", "total_share", True, "total_share", "capitalization", "实收资本(或股本)"),
]

# ⚠️ 单位差异提醒（实测 2026-10）:
#   zzshare 财报 : 元        (实测 600887 单季营收 26,660,376,531.26 = 266.6 亿)
#   zzshare 估值 : 亿元 / 万股 (market_cap=1834.67 亿, capitalization=636596 万股)
#   Tushare 财报 : 元        (官方口径)
#   本 schema 统一标 "元"，由 Value.to_yi()/to_mm() 负责换算
#   适配器层需把 zzshare 估值表的亿元转成元（×1e8）

# ============ 行情 (daily / weekly) ============
PRICE_FIELDS: list[FieldSpec] = [
    FieldSpec("交易日期", "pct", "trade_date", True, "trade_date"),
    FieldSpec("开盘价",   "元",  "open",       True, "open"),
    FieldSpec("最高价",   "元",  "high",       True, "high"),
    FieldSpec("最低价",   "元",  "low",        True, "low"),
    FieldSpec("收盘价",   "元",  "close",      True, "close"),
    FieldSpec("成交量",   "手",  "vol",        False, "vol"),
    FieldSpec("成交额",   "元",  "amount",     False, "amount"),
]

# ============ 分红 (dividend) ============
DIVIDEND_FIELDS: list[FieldSpec] = [
    FieldSpec("报告期",   "pct",  "end_date",   True, "end_date"),
    FieldSpec("公告日",   "pct",  "ann_date",   False, "ann_date"),
    FieldSpec("除权日",   "pct",  "ex_date",    False, "ex_date"),
    FieldSpec("每股股息", "元/股","cash_div_tax", True, "cash_div_tax"),
    FieldSpec("分红进度", "pct",  "div_proc",   False, "div_proc"),
    FieldSpec("送股比例", "pct",  "stk_div",    False, "stk_div"),
]

# ============ 股票基础信息 (stock_basic) ============
BASIC_FIELDS: list[FieldSpec] = [
    FieldSpec("代码",     "pct", "ts_code",   True, "ts_code"),
    FieldSpec("名称",     "pct", "name",      True, "name"),
    FieldSpec("行业",     "pct", "industry",  False, "industry"),
    FieldSpec("地区",     "pct", "area",      False, "area"),
    FieldSpec("上市日期", "pct", "list_date", False, "list_date"),
    FieldSpec("市场",     "pct", "market",    False, "market"),
]

# ============ 十大股东 (top10_holders) ============
HOLDER_FIELDS: list[FieldSpec] = [
    FieldSpec("报告期",   "pct", "end_date",     True, "end_date"),
    FieldSpec("股东名",   "pct", "holder_name",  True, "holder_name"),
    FieldSpec("持股数量", "股",  "hold_amount",  True, "hold_amount"),
    FieldSpec("持股比例", "pct", "hold_ratio",   True, "hold_ratio"),
]

# ============ 股权质押 (pledge_stat) ============
PLEDGE_FIELDS: list[FieldSpec] = [
    FieldSpec("报告期",     "pct", "end_date",      True, "end_date"),
    FieldSpec("质押比例",   "pct", "pledge_ratio",  True, "pledge_ratio"),
    FieldSpec("质押次数",   "pct", "pledge_count",  False, "pledge_count"),
    FieldSpec("无限售质押", "股",  "unrest_pledge", False, "unrest_pledge"),
    FieldSpec("限售质押",   "股",  "rest_pledge",   False, "rest_pledge"),
]

# ============ 财务审计 (fina_audit) ============
AUDIT_FIELDS: list[FieldSpec] = [
    FieldSpec("报告期",   "pct", "end_date",     True, "end_date"),
    FieldSpec("审计结果", "pct", "audit_result", True, "audit_result"),
    FieldSpec("审计机构", "pct", "audit_agency", False, "audit_agency"),
    FieldSpec("审计费用", "元",  "audit_fees",   False, "audit_fees"),
]

# ============ 主营业务构成 (fina_mainbz) ============
MAINBZ_FIELDS: list[FieldSpec] = [
    FieldSpec("报告期",   "pct",  "end_date",  True, "end_date"),
    FieldSpec("业务项目", "pct",  "bz_item",   True, "bz_item"),
    FieldSpec("营业收入", "元","bz_sales",  True, "bz_sales"),
    FieldSpec("营业成本", "元","bz_cost",   True, "bz_cost"),
    FieldSpec("营业利润", "元","bz_profit", True, "bz_profit"),
]

# ============ 无风险利率 (yc_cb) ============
RISKFREE_FIELDS: list[FieldSpec] = [
    FieldSpec("曲线日期", "pct", "trade_date", True, "trade_date"),
    FieldSpec("收益率",   "pct", "yield",      True, "yield"),
]

# ============ 股份回购 (repurchase) ============
REPURCHASE_FIELDS: list[FieldSpec] = [
    FieldSpec("公告日",   "pct",  "ann_date",   True, "ann_date"),
    FieldSpec("报告期",   "pct",  "end_date",   False, "end_date"),
    FieldSpec("回购进度", "pct",  "proc",       True, "proc"),
    FieldSpec("回购数量", "股",   "vol",        False, "vol"),
    FieldSpec("回购金额", "元",   "amount",     False, "amount"),
]


# ---------------------------------------------------------------------------
# 数据集注册表
# ---------------------------------------------------------------------------

@dataclass
class Dataset:
    """一个数据集（对应一个 API 接口）。"""

    name: str                       # 业务语义名
    tushare_api: str                # Tushare 接口名
    fields: list[FieldSpec]
    key_field: str = "报告期"        # 主键（用于多源对齐）
    freq: Literal["日", "周", "季", "年", "不定期"] = "年"

    @property
    def required_fields(self) -> list[str]:
        return [f.key for f in self.fields if f.required]

    def field_map(self, source: SourceName) -> dict[str, Optional[str]]:
        """返回 {业务名: 该源字段名} 映射。"""
        out: dict[str, Optional[str]] = {}
        for f in self.fields:
            mapped = f.native(source)
            if mapped:
                out[f.key] = mapped
        return out


DATASETS: dict[str, Dataset] = {
    "利润表":       Dataset("利润表",       "income",         INCOME_FIELDS,     "报告期", "年"),
    "资产负债表":   Dataset("资产负债表",   "balancesheet",   BALANCE_FIELDS,    "报告期", "年"),
    "现金流量表":   Dataset("现金流量表",   "cashflow",       CASHFLOW_FIELDS,   "报告期", "年"),
    "财务指标":     Dataset("财务指标",     "fina_indicator", INDICATOR_FIELDS,  "报告期", "年"),
    "每日指标":     Dataset("每日指标",     "daily_basic",    DAILY_BASIC_FIELDS,"交易日期", "日"),
    "日线行情":     Dataset("日线行情",     "daily",          PRICE_FIELDS,      "交易日期", "日"),
    "周线行情":     Dataset("周线行情",     "weekly",         PRICE_FIELDS,      "交易日期", "周"),
    "分红":         Dataset("分红",         "dividend",       DIVIDEND_FIELDS,   "报告期", "年"),
    "股票基础信息": Dataset("股票基础信息", "stock_basic",    BASIC_FIELDS,      "代码",   "不定期"),
    "十大股东":     Dataset("十大股东",     "top10_holders",  HOLDER_FIELDS,     "报告期", "季"),
    "股权质押":     Dataset("股权质押",     "pledge_stat",    PLEDGE_FIELDS,     "报告期", "周"),
    "财务审计":     Dataset("财务审计",     "fina_audit",     AUDIT_FIELDS,      "报告期", "年"),
    "主营业务构成": Dataset("主营业务构成", "fina_mainbz",    MAINBZ_FIELDS,     "报告期", "年"),
    "无风险利率":   Dataset("无风险利率",   "yc_cb",          RISKFREE_FIELDS,   "交易日期", "日"),
    "股份回购":     Dataset("股份回购",     "repurchase",     REPURCHASE_FIELDS, "报告期", "不定期"),
}


def get_dataset(name: str) -> Dataset:
    if name not in DATASETS:
        raise KeyError(f"未知数据集 {name!r}；可用：{list(DATASETS)}")
    return DATASETS[name]


# 文本类字段（不参与数值计算，仅标识/展示）
STRING_FIELDS: set[str] = {
    # 标识
    "代码", "名称", "行业", "地区", "市场",
    # 股东
    "股东名", "股份类型", "增减",
    # 分红/回购
    "分红进度", "回购进度", "业务项目",
    # 审计
    "审计结果", "审计机构",
    # 质押
    "所属行业",
}


def is_string_field(key: str) -> bool:
    return key in STRING_FIELDS


# 补充 AkShare 列名（单独文件维护，避免本文件过长）
from . import akshare_columns  # noqa: E402

AKSHARE_FIELD_COUNT = akshare_columns.apply()

# 标记文本类字段
for _ds in DATASETS.values():
    for _f in _ds.fields:
        if _f.key in STRING_FIELDS:
            object.__setattr__(_f, "kind", "str")


# ---------------------------------------------------------------------------
# 记录与结果容器
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """一行数据：{业务字段名: Value}。"""

    values: dict[str, Value] = field(default_factory=dict)
    source: Optional[SourceName] = None

    def get(self, key: str) -> Value:
        return self.values.get(key, Value(None, note=f"字段 {key} 不存在"))

    def __getitem__(self, key: str) -> Value:
        return self.get(key)

    def to_dict(self) -> dict[str, Any]:
        return {k: asdict(v) for k, v in self.values.items()}


@dataclass
class Result:
    """一次查询的结果（某个数据集的多行）。"""

    dataset: str
    source: SourceName
    records: list[Record] = field(default_factory=list)
    fetched_at: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def __getitem__(self, idx: int) -> Record:
        return self.records[idx]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    def missing_required(self) -> list[str]:
        """返回缺失的必需字段（去重）。"""
        ds = get_dataset(self.dataset)
        missing: set[str] = set()
        for r in self.records:
            for k in ds.required_fields:
                if r.get(k).missing:
                    missing.add(k)
        return sorted(missing)

