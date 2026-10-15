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

    @property
    def missing(self) -> bool:
        return self.value is None

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
        if self.missing:
            return f"Value(MISSING, note={self.note!r})"
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

    def native(self, source: str) -> Optional[str]:
        """返回该源的原生字段名。zzshare 兼容 Tushare，未显式指定时回退。"""
        explicit = getattr(self, source, None)
        if explicit:
            return explicit
        if source == "zzshare":
            return self.tushare          # zzshare 字段名兼容 Tushare
        return None


# ============ 利润表 (income) ============
INCOME_FIELDS: list[FieldSpec] = [
    FieldSpec("营业收入",   "百万元", "revenue",                True,  "revenue"),
    FieldSpec("营业成本",   "百万元", "oper_cost",              True,  "oper_cost"),
    FieldSpec("研发费用",   "百万元", "rd_exp",                 False, "rd_exp"),
    FieldSpec("销售费用",   "百万元", "sell_exp",               False, "sell_exp"),
    FieldSpec("管理费用",   "百万元", "admin_exp",              False, "admin_exp"),
    FieldSpec("财务费用",   "百万元", "finance_exp",            False, "finance_exp"),
    FieldSpec("经营利润",   "百万元", "operate_profit",         False, "operate_profit"),
    FieldSpec("利润总额",   "百万元", "total_profit",           False, "total_profit"),
    FieldSpec("所得税",     "百万元", "income_tax",             True,  "income_tax"),
    FieldSpec("净利润",     "百万元", "n_income",               False, "n_income"),
    FieldSpec("归母净利润", "百万元", "n_income_attr_p",        True,  "n_income_attr_p"),
    FieldSpec("少数股东损益","百万元","minority_gain",          True,  "minority_gain"),
    FieldSpec("扣非归母净利润","百万元","profit_dedt",          False, "profit_dedt"),
    FieldSpec("基本EPS",    "元/股",  "basic_eps",              False, "basic_eps"),
]

# ============ 资产负债表 (balancesheet) ============
BALANCE_FIELDS: list[FieldSpec] = [
    FieldSpec("货币资金",   "百万元", "money_cap",              True,  "money_cap"),
    FieldSpec("交易性金融资产","百万元","trad_asset",           False, "trad_asset"),
    FieldSpec("应收账款",   "百万元", "accounts_receiv",        True,  "accounts_receiv"),
    FieldSpec("存货",       "百万元", "inventories",            False, "inventories"),
    FieldSpec("合同负债",   "百万元", "contract_liab",          True,  "contract_liab"),
    FieldSpec("应付账款",   "百万元", "acct_payable",           True,  "acct_payable"),
    FieldSpec("商誉",       "百万元", "goodwill",               False, "goodwill"),
    FieldSpec("递延所得税资产","百万元","defer_tax_assets",     False, "defer_tax_assets"),
    FieldSpec("递延所得税负债","百万元","defer_tax_liab",       False, "defer_tax_liab"),
    FieldSpec("总资产",     "百万元", "total_assets",           True,  "total_assets"),
    FieldSpec("总负债",     "百万元", "total_liab",             True,  "total_liab"),
    FieldSpec("归母权益",   "百万元", "total_hldr_eqy_exc_min_int", True, "total_hldr_eqy_exc_min_int"),
    FieldSpec("总股本",     "股",     "total_share",            True,  "total_share"),
]

# ============ 现金流量表 (cashflow) ============
CASHFLOW_FIELDS: list[FieldSpec] = [
    FieldSpec("经营活动现金流", "百万元", "n_cashflow_act",           True,  "n_cashflow_act"),
    FieldSpec("资本开支",       "百万元", "c_pay_acq_const_fiolta",    True,  "c_pay_acq_const_fiolta"),
    FieldSpec("折旧摊销_固定资产","百万元","depr_fa_coga_dpba",       True,  "depr_fa_coga_dpba"),
    FieldSpec("摊销_无形资产",  "百万元", "amort_intang_assets",       True,  "amort_intang_assets"),
    FieldSpec("摊销_长期待摊",  "百万元", "lt_amort_deferred_exp",     True,  "lt_amort_deferred_exp"),
    FieldSpec("已付股息",       "百万元", "c_pay_dist_dpcp_int_exp",   True,  "c_pay_dist_dpcp_int_exp"),
    FieldSpec("支付给职工现金", "百万元", "c_pay_to_staff",            False, "c_pay_to_staff"),
]

# ============ 财务指标 (fina_indicator) ============
INDICATOR_FIELDS: list[FieldSpec] = [
    FieldSpec("加权ROE",    "pct", "roe_waa",         True,  "roe_waa"),
    FieldSpec("毛利率",     "pct", "grossprofit_margin", False, "grossprofit_margin"),
    FieldSpec("净利率",     "pct", "netprofit_margin", False, "netprofit_margin"),
    FieldSpec("每股净资产", "元/股","bps",             False, "bps"),
    FieldSpec("资产负债率", "pct", "debt_to_assets",  False, "debt_to_assets"),
]

# ============ 每日指标 (daily_basic) ============
DAILY_BASIC_FIELDS: list[FieldSpec] = [
    FieldSpec("收盘价",   "元",   "close",      True,  "close"),
    FieldSpec("总市值",   "元",   "total_mv",   True,  "total_mv"),
    FieldSpec("流通市值", "元",   "circ_mv",    False, "circ_mv"),
    FieldSpec("PE_TTM",   "pct",  "pe_ttm",     True,  "pe_ttm"),
    FieldSpec("PB",       "pct",  "pb",         False, "pb"),
    FieldSpec("股息率TTM","pct",  "dv_ttm",     False, "dv_ttm"),
    FieldSpec("总股本",   "股",   "total_share", True, "total_share"),
]

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
    FieldSpec("营业收入", "百万元","bz_sales",  True, "bz_sales"),
    FieldSpec("营业成本", "百万元","bz_cost",   True, "bz_cost"),
    FieldSpec("营业利润", "百万元","bz_profit", True, "bz_profit"),
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
