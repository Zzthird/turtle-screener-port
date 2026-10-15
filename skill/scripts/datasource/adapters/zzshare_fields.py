"""zzshare 实测字段映射（2026-10 实测确认）。

⚠️ 关键发现：
1. zzshare 财报表返回**单季度值**，不是累计值 → 必须按 statDate 分组累加
2. code 格式与 Tushare 一致（600887.SH），可直接过滤
3. 字段名与 Tushare **完全不同**（用 zzshare 的原生名）
4. finance_stock(table=..., code=...) 是单股历史查询，一次返回多年多季

实测验证（600887.SH）：
  zzshare 单季累加 2024 → 营收 1153.93 亿 / 归母 84.53 亿
  官方年报          2024 → 营收 1157.80 亿 / 归母 84.53 亿
  归母完全吻合，营收差 0.33%（口径差异，zzshare 用 operating_revenue 非 total）

Tushare 接口名 -> zzshare 查询参数
"""

# Tushare 接口名 -> finance_stock 的 table 参数
TABLE_MAP = {
    "income":         "income",
    "balancesheet":   "balance",
    "cashflow":       "cash_flow",
    "fina_indicator": "indicator",
    "daily_basic":    "valuation",
}

# ---- 利润表 ----
INCOME_MAP = {
    "营业收入":     "operating_revenue",            # 主营口径
    "营业收入_总":  "total_operating_revenue",       # 含其他业务
    "营业成本":     "operating_cost",
    "营业税金":     "operating_tax",
    "销售费用":     "sale_expense",
    "管理费用":     "management_expense",
    "财务费用":     "financial_expense",
    "研发费用":     "research_expense",
    "经营利润":     "operating_profit",
    "利润总额":     "total_profit",
    "所得税":       "income_tax",
    "净利润":       "net_profit",
    "归母净利润":   "np_parent_company_owners",
    "少数股东损益": "minority_interest",
    "扣非归母净利润":"deduct_parent_net_profit",
    "投资收益":     "investment_income",
    "营业外收入":   "non_operating_income",
    "营业外支出":   "non_operating_expense",
}

# ---- 资产负债表 ----
BALANCE_MAP = {
    "货币资金":     "cash_equivalents",
    "交易性金融资产":"trading_financial_assets",
    "应收账款":     "accounts_receivable",
    "应收票据":     "notes_receivable",
    "预付款项":     "prepayments",
    "其他应收款":   "other_receivables",
    "存货":         "inventory",
    "流动资产合计": "total_current_assets",
    "长期股权投资": "long_term_equity_investment",
    "固定资产":     "fixed_assets",
    "在建工程":     "construction_in_progress",
    "无形资产":     "intangible_assets",
    "商誉":         "goodwill",
    "投资性房地产": "investment_property",
    "总资产":       "total_assets",
    "短期借款":     "short_term_loan",
    "应付账款":     "accounts_payable",
    "应付票据":     "notes_payable",
    "预收款项":     "advance_receipts",
    "合同负债":     "contract_liabilities",
    "应付职工薪酬": "salaries_payable",
    "应交税费":     "taxes_payable",
    "长期借款":     "long_term_loan",
    "应付债券":     "bonds_payable",
    "流动负债合计": "total_current_liability",
    "非流动负债合计":"total_non_current_liability",
    "总负债":       "total_liability",
    "实收资本":     "paid_in_capital",
    "资本公积":     "capital_reserve",
    "盈余公积":     "surplus_reserve",
    "未分配利润":   "undistributed_profit",
    "所有者权益合计":"total_owner_equities",
    "归母权益":     "owners_equity_including_minority_interest",
    "少数股东权益": "minority_interest",
    "递延所得税资产":"deferred_tax_assets",
    "递延所得税负债":"deferred_tax_liabilities",
}

# ---- 现金流量表 ----
CASHFLOW_MAP = {
    "经营活动现金流":   "net_operate_cash_flow",
    "投资活动现金流":   "net_invest_cash_flow",
    "融资活动现金流":   "net_finance_cash_flow",
    "资本开支":         "buy_construct_long_term_assets",
    "销售商品收到现金": "sale_goods_services",
    "处置长期资产收回": "dispose_long_term_assets",
    "分配股利支付现金": "cash_dividend_internal",
    "期初现金":         "begin_cash_balance",
    "期末现金":         "end_cash_balance",
    "现金净增加额":     "cash_and_equivalents_increase",
    "汇率变动影响":     "exchange_rate_effect",
}

# ---- 财务指标 ----
INDICATOR_MAP = {
    "加权ROE":  "roe",                        # 注意：返回的是小数(0.97=97%)还是百分数？实测 0.97
    "毛利率":   "gross_profit_margin",
    "净利率":   "net_profit_margin",
    "每股收益": "eps",
    "营收增速": "inc_revenue_year_on_year",
    "净利增速": "inc_net_profit_year_on_year",
    "扣非净利": "adjusted_profit",
}

# ---- 估值（finance_valuation）----
VALUATION_MAP = {
    "市盈率":   "pe_ratio",
    "市净率":   "pb_ratio",
    "市销率":   "ps_ratio",
    "市现率":   "pcf_ratio",
    "总市值":   "market_cap",             # 单位：亿元
    "流通市值": "circulating_market_cap",  # 单位：亿元
    "总股本":   "capitalization",          # 单位：万股
    "流通股本": "circulating_cap",
    "换手率":   "turnover_ratio",
}

ALL_MAPS = {
    "income": INCOME_MAP,
    "balancesheet": BALANCE_MAP,
    "cashflow": CASHFLOW_MAP,
    "fina_indicator": INDICATOR_MAP,
    "daily_basic": VALUATION_MAP,
}

# 财务表返回单季度值，必须累加为年度
QUARTERLY_CUMULATIVE = {"income", "cashflow", "fina_indicator"}
