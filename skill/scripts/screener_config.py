"""Configuration for Turtle Screener (龟龟选股器).

Defines ScreenerConfig dataclass with all tunable thresholds, scoring weights,
and cache settings for Tier 1 / Tier 2 screening pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ScreenerConfig:
    """All tunable parameters for the screener pipeline.

    Tier 1 filters (market data only, applied to full A-share universe):
    - Excludes ST/PT/退市整理 stocks
    - Requires listing age >= min_listing_years
    - Requires market cap >= min_market_cap_yi (亿元)
    - Requires daily turnover >= min_turnover_pct (%)
    - Requires 0 < PB <= max_pb
    - Requires dividend yield > 0

    Tier 1 dual-channel PE:
    - Main channel: 0 < PE_TTM <= max_pe
    - Observation channel: PE_TTM < 0, top obs_channel_limit by market cap

    Tier 1 ranking:
    - Composite score = dv_weight * dv_ttm + pe_weight * (1/PE) + pb_weight * (1/PB)
    - Main channel takes top tier2_main_limit
    - Total into Tier 2: tier2_main_limit + obs_channel_limit

    Tier 2 hard vetoes:
    - Pledge ratio > tier1_max_pledge → reject（已前移 Tier1）
    - Non-standard audit opinion → reject

    Tier 2 financial quality:
    - ROE(weighted) >= min_roe (%)
    - Gross margin >= min_gross_margin (%)
    - Debt-to-assets <= max_debt_ratio (%)

    Scoring weights (must sum to 1.0):
    - ROE, FCF yield, penetration R, EV/EBITDA (inverse), floor premium (inverse)
    """

    # --- Tier 1: Hard filters ---
    min_listing_years: int = 3
    min_market_cap_yi: float = 5.0  # 亿元
    min_turnover_pct: float = 0.1   # %
    max_pb: float = 10.0
    max_pe: float = 50.0
    include_bank: bool = True  # 是否包含银行股（True=包含）
    # 上游 notebook 设计意图：「对于银行等高杠杆行业，max_debt_ratio=70% 会自动放宽」
    # 实际上免费源取不到银行的 grossprofit_margin / debt_to_assets（本就无意义），
    # 值为 None → 质量门槛自动跳过 → 银行仅受 ROE 约束。故默认包含银行。
    # 框架对银行的正确指标（NIM/不良率/拨备覆盖率）属定性分析职责，
    # 见 shared/qualitative/references/industry_metrics_lookup.md，筛选器不计算。

    # --- Tier 1: Dual-channel PE ---
    obs_channel_limit: int = 50

    # --- Tier 1: Ranking & cutoff ---
    tier2_main_limit: int = 150
    # 是否在 Tier1 末尾按 R 分【截断】到 tier2_main_limit。
    # False（默认）= 所有通过硬门槛的股票全部进 Tier2（只排序，不截断）。
    #   理由：粗算 R 已含否决门，再截断会丢掉 R 达标但排名靠后的标的；
    #         且 Tier2 现在已去掉重复门槛，单只成本比原先低。
    # True = 恢复旧行为（只取 Top tier2_main_limit 进 Tier2）。
    tier1_truncate: bool = False
    # 把 Tier2 的质量硬门槛前移到 Tier1（ROE/毛利率/负债率）。
    # 依据：这三项均可用全市场批量源一次拿到（zzshare finance_indicator，
    # ~0.8s 覆盖 5184 只），提前过滤可避免低质量股占用 Tier2 名额。
    # 而 R / FCF / EV/EBITDA / 底价依赖多年多表，仍留在 Tier2。
    tier1_apply_quality: bool = True
    # 支付率上限：> 100% 视为借债/透支利润分红 → Tier1 剔除。
    # 数据源：智兔 /hicw/yjbb 的 fpfa（分红方案）× mgsy（每股收益），全市场批量。
    # 仅对 main 通道生效（观察通道本就无股息要求）。
    tier1_max_payout: float = 100.0
    # --- Tier 1: 框架因子2 粗算穿透回报率 R ---
    # R = 归母净利 C × 支付率 M / 市值（框架原生「粗算 R」）
    # 上游放 Tier2 因 Tushare 需逐股；免费源可批量 → 下放到 Tier1。
    tier1_apply_R: bool = True
    tier1_R_floor_mult: float = 0.5   # 否决门：R ≥ II × 0.5（框架规则）
    tier1_R_rank: bool = True         # 排序是否改用 R_coarse（否则用原三因子打分）
    # --- Tier 1: 质押硬否决（框架 Tier2 硬否决前移）---
    # 数据源：AkShare stock_gpzy_pledge_ratio_em（全市场 2294 行，0.7s）
    # 未出现在表中的股票 = 无质押 = 放行。
    tier1_apply_pledge: bool = True
    tier1_max_pledge: float = 70.0

    # --- Tier 2: 是否重做质量门槛 ---
    # 默认 False：ROE/毛利率/负债率 已在 Tier1 用全市场批量源筛过，
    # Tier2 再判属重复（浪费 150 只 × 逐股请求）。
    # 置 True 可恢复旧行为（调试 / 对比用）。
    tier2_recheck_quality: bool = False
    # --- Tier 2: 框架粗算 R 否决门 ---
    # 用【精算 R（AA 口径）】再确认一次。
    #
    # 为何【开】（2026-10 修正）：
    #   早期担心与 Tier1 的粗算 R 门槛「双重剔除」故默认关，但该理由不成立 ——
    #   粗算 R 用归母净利 C，精算 R 用 AA（真实可支配现金），两者【不是同一个指标】。
    #   框架原文本就是「因子2 粗算 → 因子3 精算」两道结构。
    #   实测（Tier2 前 20 只）有 3 只粗算达标但精算为负：
    #     好想你 6.41/-0.55、天地科技 6.04/-0.66、宁波高发 6.23/-2.15
    #   —— 正是「净利好看但真实现金为负」的伪穿透回报，不应放行。
    tier2_apply_R_veto: bool = True
    dv_weight: float = 0.4
    pe_weight: float = 0.3
    pb_weight: float = 0.3

    # --- Tier 2: Hard vetoes ---
    # 注：质押硬否决已前移至 Tier1（tier1_apply_pledge / tier1_max_pledge）。
    # Tier2 只剩【审计意见非标】一项（免费源无法批量拿审计意见）。
    #
    # 框架因子2【分配能力验证】一票否决：
    #   FCF 长期为负 且 融资CF 持续为正 → 伪穿透回报 → 否决
    tier2_apply_alloc_veto: bool = True
    # --- Tier 2: 支付率锚定窗口 ---
    # 框架原文（因子2 步骤5）：「过去 3~5 年支付率序列」。
    # 默认 5 —— 与 datapack 的 §17.10 口径一致，便于交叉验证。
    # 上游 screener_core 原本用 3。
    tier2_M_years: int = 5

    # --- 质量门槛（2026-02 换源重写）------------------------------
    # 数据源（均为全市场批量，无需逐股）：
    #   ROE/毛利率 -> AkShare stock_yjbb_em（东财【加权】年报口径）
    #  负债率     -> zzshare balance 自算（含银行）
    #   速动比率   -> 智兔 /hicw/cznl（银行整条缺记录 → 豁免）
    #
    # 东财接口节流（重要）：
    #   stock_yjbb_em / stock_fhps_em 均直连东财数据中心，内部【逐页翻页】
    #   均为【真实 HTTP 请求】（非本地分页；实测 yjbb 24 页、fhps 8 页/年）。
    #   backend 的 _em_throttle() + _em_paged() 对【调用前 + 每页】均加间隔
    #   （默认 1s，共 ~64 页 -> ~64s）。
    #   可用 RATE_LIMIT_EM_INTERVAL 覆盖（设 0 则不节流）。
    #
    # ⚠️ 【OCF 质量门槛已删除】(2026-02)：原设计为「M 高时校验
    #   Σ5年OCF ≥ Σ5年分红」，因 zzshare 的 OCF 数据错误而移除
    #   （实测宇通 2023：zzshare -4.60亿 vs 实际 +47.17亿，符号相反）。
    #   详见 screener_backend.ocf_all 的 docstring。
    #
    #   min_roe        : 8.0。旧值 6.0 是因为当时用 zzshare 单季×4 累加，
    #                    口径不准且存在部分相加 bug，所以故意压低。
    #                    现改用 yjbb_em 加权年报值（实测伊利15.81/神华13.55/
    #                    招行14.49，与人工核对值一致），门槛可以设准。
    #   min_roe_refined: 保留字段以兼容旧消费方；Tier2 细筛已改为
    #                    【复用 Tier1 值】，不再重算（见 tier2 的 Step 2c）。
    min_roe: float = 8.0             # % (Tier1，源已换 stock_yjbb_em 加权口径)
    min_roe_refined: float = 8.0     # % (Tier2 细筛，新浪年报) —— 保留兼容
    refined_roe_apply: bool = True               # 是否启用 Tier2 细筛
    min_gross_margin: float = 15.0   # %
    max_debt_ratio: float = 70.0     # %
    # 速动比率下限（2026-02 新增）—— Tier1 无条件硬门槛。
    # 来源：智兔 /hicw/cznl 的 sdbl（批量 5000 只）。实测：神华2.08/宇通1.21/伊利0.62
    # 豁免：金融股跳过（银行在 cznl 中整条缺记录，且存款即流动负债，指标不适用）
    min_quick_ratio: float = 0.8

    # --- Tier2 分红质量②：有息负债/净资产【已禁用】2026-02 ---
    # 🚫 禁用原因：有息负债科目【全链路拿不到】。实测 600887 伊利：
    #   balancesheet 实际返回无 st_borr / lt_borr / bond_payable；
    #   datasource.get("资产负债表") 仅 13 字段同样无借款；
    #   zzshare finance_balance 借款类全空；stock_zcfz_em 无此列。
    #   故该门槛对全部股票都是 'missing'，形同虚设。
    # ✅ 同类风险已由 Tier1 的资产负债率 ≤70%（银行 96%）覆盖。
    tier2_apply_debt_quality: bool = False
    max_interest_debt_to_equity: float = 50.0   # % （仅保留供将来启用）

    # --- Tier 2: 银行/高杠杆行业专用门槛 ---
    # 上游 notebook 的设计意图：「对于银行等高杠杆行业，max_debt_ratio=70%
    # 会自动放宽」——但上游代码里并未实现。此处补上。
    # 行业名单按【申万一级】名称匹配（智兔行业源），兼容旧口径「银行/金融」。
    bank_industries: tuple = (
        "银行", "非银金融", "货币金融", "金融业", "保险", "证券",
    )
    max_debt_ratio_bank: float = 96.0    # 银行/非银：存款即负债，90%+ 是常态
    min_gross_margin_bank: float = 0.0   # 银行无「营业成本」概念，毛利率不适用

    # --- Tier 2: Observation channel quality (relaxed vs main) ---
    # 观察通道 = 亏损股（负 PE → 负 ROE），ROE 门槛【无法】设正下限：
    # 原框架设 0% 会把整条通道清空（亏损股 ROE 全为负）。故默认 -999
    # 等效禁用，只靠 毛利率/负债率 + OCF/FCF 信号把关。
    min_roe_obs: float = -999.0             # 等效禁用（亏损股 ROE 天然为负）
    min_fcf_margin_obs: float = 0.0         # FCF/Revenue >= 0%
    min_fcf_positive_years_obs: int = 2     # FCF positive in >= 2 of 5 years
    obs_require_ocf_positive: bool = True   # Latest year OCF > 0

    # --- Scoring weights ---
    weight_roe: float = 0.20
    weight_fcf_yield: float = 0.20
    weight_penetration_r: float = 0.25
    weight_ev_ebitda: float = 0.15
    weight_floor_premium: float = 0.20

    # --- Cache ---
    cache_dir: str = "output/.screener_cache"
    cache_stock_basic_ttl_days: int = 7
    cache_daily_basic_ttl_days: int = 0  # 0 = same-day only
    cache_rf_ttl_days: int = 1
    cache_tier2_ttl_hours: int = 24
    cache_tier2_financial_ttl_hours: int = 168  # 7天，年报类数据
    cache_tier2_market_ttl_hours: int = 24      # 1天，周线行情
    cache_tier2_global_ttl_hours: int = 24      # 1天，yc_cb 无风险利率

    @property
    def tier2_max_stocks(self) -> int:
        """Total stocks entering Tier 2 = main + observation."""
        return self.tier2_main_limit + self.obs_channel_limit

    @property
    def scoring_weights(self) -> dict[str, float]:
        """Return scoring weights as a dict."""
        return {
            "roe": self.weight_roe,
            "fcf_yield": self.weight_fcf_yield,
            "penetration_r": self.weight_penetration_r,
            "ev_ebitda": self.weight_ev_ebitda,
            "floor_premium": self.weight_floor_premium,
        }

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of error messages (empty = OK)."""
        errors = []
        w_sum = (self.weight_roe + self.weight_fcf_yield +
                 self.weight_penetration_r + self.weight_ev_ebitda +
                 self.weight_floor_premium)
        if abs(w_sum - 1.0) > 0.01:
            errors.append(f"Scoring weights must sum to 1.0, got {w_sum:.3f}")
        if self.min_listing_years < 0:
            errors.append("min_listing_years must be >= 0")
        if self.min_market_cap_yi < 0:
            errors.append("min_market_cap_yi must be >= 0")
        if self.tier2_main_limit < 1:
            errors.append("tier2_main_limit must be >= 1")
        if self.obs_channel_limit < 0:
            errors.append("obs_channel_limit must be >= 0")
        if self.min_fcf_positive_years_obs < 0 or self.min_fcf_positive_years_obs > 5:
            errors.append("min_fcf_positive_years_obs must be 0-5")
        return errors

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for display/serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScreenerConfig":
        """Create from dict, ignoring unknown keys."""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)
