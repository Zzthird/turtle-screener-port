#!/usr/bin/env python3
"""Turtle Screener (龟龟选股器) — Core screening logic.

Two-tier architecture:
  Tier 1: Bulk market data screening (~5s, 2 API calls)
  Tier 2: Per-stock deep analysis (~5s/stock)

Usage:
    python scripts/screener_core.py --tier1-only
    python scripts/screener_core.py --tier2-limit 50
    python scripts/screener_core.py --min-roe 15 --max-pe 30 --csv output/results.csv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from config import get_token
from screener_config import ScreenerConfig

# Lazy import to avoid circular dependency at module level
_TushareClient = None


def _get_tushare_client_class():
    global _TushareClient
    if _TushareClient is None:
        from tushare_client import TushareClient
        _TushareClient = TushareClient
    return _TushareClient


def _sf(val):
    """安全转 float：None / NaN / 非数值 -> None。

    模块级版本（各方法内原有同名局部函数保持不变，互不干扰）。
    """
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # 排除 NaN


# ============================================================
# Tier 2 field supersets (union of all consumers per API)
# ============================================================

_TIER2_FIELDS = {
    "income": "ts_code,end_date,n_income_attr_p,operate_profit,fin_exp,non_oper_income,oth_income,asset_disp_income,revenue",
    "balancesheet": ("ts_code,end_date,money_cap,trad_asset,st_borr,lt_borr,"
                     "bond_payable,non_cur_liab_due_1y,goodwill,total_assets,"
                     "total_hldr_eqy_exc_min_int"),
    "cashflow": ("ts_code,end_date,n_cashflow_act,c_pay_acq_const_fiolta,"
                 "depr_fa_coga_dpba,amort_intang_assets,lt_amort_deferred_exp"),
    "dividend": "ts_code,end_date,cash_div_tax,base_share",
    "fina_indicator": ("ts_code,end_date,roe_waa,grossprofit_margin,"
                       "debt_to_assets,profit_dedt,"
                       "ebitda,fcff,netdebt,interestdebt"),
    "fina_audit": "ts_code,end_date,audit_result",
    "weekly": "ts_code,trade_date,close",
    "yc_cb": "trade_date,yield",
}

# TTL category for each Tier 2 API
_TIER2_TTL_CATEGORY = {
    "income": "financial",
    "balancesheet": "financial",
    "cashflow": "financial",
    "dividend": "financial",
    "fina_indicator": "financial",
    "fina_audit": "financial",
    "weekly": "market",
    "yc_cb": "global",
}

# ============================================================
# Cache
# ============================================================

# ScreenerCache now lives in cache_utils; re-exported here so existing
# imports (`from screener_core import ScreenerCache`) keep working.
from cache_utils import ScreenerCache


# ============================================================
# TushareScreener
# ============================================================


class TushareScreener:
    """Main screener class: Tier 1 bulk screening + Tier 2 deep analysis."""

    def __init__(self, token: str | None = None, config: ScreenerConfig | None = None):
        self.config = config or ScreenerConfig()
        self.cache = ScreenerCache(self.config.cache_dir)
        self._rf_cache: float | None = None  # global risk-free rate
        # Tier1 算出的 M 分量缓存（Tier2 复用，避免重算与口径不一致）
        self._tier1_M_cache: dict[str, dict] = {}
        # 当前股票被否决的原因（_analyze_single_stock 返回 None 时供调用方读取）
        self._last_veto: str = ""
        # Tier2 被否决清单（含真实原因，供审计/导出）
        self._tier2_vetoed: pd.DataFrame = pd.DataFrame()
        self._stock_data_cache: dict[str, pd.DataFrame] = {}  # per-stock in-memory cache
        # 数据后端：有 token 走 Tushare，否则用免费源（AkShare/东财）
        self._backend = None
        self._token = token or os.environ.get("TUSHARE_TOKEN", "")
        if not self._token:
            try:
                from config import get_token
                self._token = get_token()
            except Exception:                         # noqa: BLE001
                self._token = ""
        if not self._token:
            from screener_backend import make_backend
            self._backend = make_backend(verbose=os.environ.get("SCREENER_VERBOSE") == "1")
            print("[screener] 未配置 TUSHARE_TOKEN -> 使用免费数据源")
        self._pro = None  # lazy init

    def _get_pro(self):
        """Lazy-initialize Tushare pro API."""
        if self._pro is None:
            import tushare as ts
            ts.set_token(self._token)
            self._pro = ts.pro_api(timeout=30)
            api_url = os.environ.get("TUSHARE_API_URL", "")
            if api_url:
                self._pro._DataApi__http_url = api_url
        return self._pro

    def _safe_call(self, api_name: str, **kwargs) -> pd.DataFrame:
        """Call data API (Tushare or free backend)."""
        if self._backend is not None:
            return self._free_call(api_name, **kwargs)
        pro = self._get_pro()
        last_err = None
        try:
            rate_delay = float(os.environ.get("TUSHARE_RATE_DELAY", "0.5"))
        except ValueError:
            rate_delay = 0.5
        for attempt in range(1, 4):
            try:
                if rate_delay > 0:
                    time.sleep(rate_delay)
                api_func = getattr(pro, api_name)
                return api_func(**kwargs)
            except Exception as e:
                last_err = e
                if attempt < 3:
                    import tushare as ts
                    self._pro = ts.pro_api(timeout=30)
                    api_url = os.environ.get("TUSHARE_API_URL", "")
                    if api_url:
                        self._pro._DataApi__http_url = api_url
                    time.sleep(1.0 * attempt)
        raise RuntimeError(f"Tushare API '{api_name}' failed after 3 retries: {last_err}")

    def _free_call(self, api_name: str, **kwargs) -> pd.DataFrame:
        """Tushare 接口名 -> 免费后端映射。"""
        b = self._backend
        ts_code = kwargs.get("ts_code")
        if api_name == "stock_basic":
            snap = b.snapshot()
            # 确保 screener 需要的列都存在（缺失补空）
            for c in ("ts_code", "name", "industry", "area", "market", "list_date"):
                if c not in snap.columns:
                    snap[c] = ""
            cols = ["ts_code", "name", "industry", "area", "market", "list_date"]
            return snap[cols].copy()
        if api_name == "daily_basic":
            snap = b.snapshot()
            for c in ("ts_code", "trade_date", "close", "pe_ttm", "pb",
                      "total_mv", "circ_mv", "dv_ttm", "turnover_rate"):
                if c not in snap.columns:
                    snap[c] = None
            keep = ["ts_code", "trade_date", "close", "pe_ttm", "pb",
                    "total_mv", "circ_mv", "dv_ttm", "turnover_rate"]
            df = snap[keep].copy()
            td = kwargs.get("trade_date")
            if td is not None:
                df["trade_date"] = td
            return df
        if api_name == "fina_indicator":
            if ts_code:
                return b.fina_indicator(ts_code)
            return pd.DataFrame()
        if api_name in ("income", "cashflow", "balancesheet", "dividend", "yc_cb"):
            return b.dataset_call(api_name, ts_code, **kwargs)
        if api_name == "weekly":
            # 周线：zzshare 日线聚合（避开东财 push2his），详见 _zzshare_weekly
            return b.dataset_call("weekly", ts_code, **kwargs)
        if api_name == "trade_cal":
            return b.trade_cal(kwargs.get("start_date", ""), kwargs.get("end_date", ""))
        # 未映射的接口：返回空表（调用方应能容忍）
        return pd.DataFrame()

    def _cached_call(self, api_name: str, ts_code: str | None = None,
                     **kwargs) -> pd.DataFrame:
        """Wrapper around _safe_call with in-memory + disk caching.

        Lookup order: memory dict → disk Parquet → API call.
        Uses _TIER2_FIELDS superset for the API's fields parameter.
        """
        # Build cache key
        if ts_code is not None:
            cache_key = f"tier2_{ts_code}_{api_name}"
        else:
            cache_key = f"global_{api_name}"

        # 1. Check in-memory cache
        if cache_key in self._stock_data_cache:
            return self._stock_data_cache[cache_key]

        # 2. Determine TTL
        category = _TIER2_TTL_CATEGORY.get(api_name, "financial")
        cfg = self.config
        if category == "financial":
            ttl_seconds = cfg.cache_tier2_financial_ttl_hours * 3600
        elif category == "market":
            ttl_seconds = cfg.cache_tier2_market_ttl_hours * 3600
        else:  # global
            ttl_seconds = cfg.cache_tier2_global_ttl_hours * 3600

        # 3. Check disk cache
        disk_df = self.cache.get(cache_key, ttl_seconds)
        if disk_df is not None:
            self._stock_data_cache[cache_key] = disk_df
            return disk_df

        # 4. Call API with superset fields
        call_kwargs = dict(kwargs)
        if api_name in _TIER2_FIELDS:
            call_kwargs["fields"] = _TIER2_FIELDS[api_name]
        if ts_code is not None:
            call_kwargs["ts_code"] = ts_code

        df = self._safe_call(api_name, **call_kwargs)

        # Rename Tushare API fields to project internal names
        if api_name == "income" and not df.empty:
            df.rename(columns={"fin_exp": "finance_exp"}, inplace=True)

        # 5. Cache non-empty results
        if not df.empty:
            self._stock_data_cache[cache_key] = df
            self.cache.put(cache_key, df)

        return df

    def _clear_stock_cache(self, ts_code: str) -> None:
        """Clear in-memory cache entries for a single stock. Disk cache is preserved."""
        prefix = f"tier2_{ts_code}_"
        keys_to_remove = [k for k in self._stock_data_cache if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._stock_data_cache[k]

    # ---- Tier 1: Bulk data ----

    def _get_latest_trade_date(self) -> str:
        """Get the latest trading date with fully-populated daily_basic data.

        Before 19:00, today's data may not be ready (dv_ttm etc. are None),
        so we use yesterday as the end_date to get the previous trade date.
        """
        now = datetime.now()
        if now.hour < 19:
            end = (now - timedelta(days=1)).strftime("%Y%m%d")
        else:
            end = now.strftime("%Y%m%d")
        start = (now - timedelta(days=10)).strftime("%Y%m%d")
        df = self._safe_call("trade_cal", exchange="SSE",
                             start_date=start, end_date=end,
                             fields="cal_date,is_open")
        if df.empty:
            return end
        open_days = df[df["is_open"] == 1].sort_values("cal_date", ascending=False)
        if open_days.empty:
            return end
        return open_days.iloc[0]["cal_date"]

    def _tier1_bulk_data(self, force_refresh: bool = False) -> pd.DataFrame:
        """Fetch full A-share universe: stock_basic + daily_basic merged.

        Returns merged DataFrame with columns from both APIs.
        Uses cache with configurable TTL.
        """
        cfg = self.config
        trade_date = self._get_latest_trade_date()

        # --- stock_basic ---
        sb_key = "stock_basic_all"
        sb_ttl = cfg.cache_stock_basic_ttl_days * 86400
        stock_df = None if force_refresh else self.cache.get(sb_key, sb_ttl)
        if stock_df is None:
            stock_df = self._safe_call(
                "stock_basic",
                fields="ts_code,name,industry,area,market,list_date"
            )
            if not stock_df.empty:
                self.cache.put(sb_key, stock_df)

        # --- daily_basic ---
        db_key = f"daily_basic_{trade_date}"
        # same-day cache: TTL = rest of day (use 18 hours as proxy)
        db_ttl = 18 * 3600 if cfg.cache_daily_basic_ttl_days == 0 else cfg.cache_daily_basic_ttl_days * 86400
        daily_df = None if force_refresh else self.cache.get(db_key, db_ttl)
        if daily_df is None:
            daily_df = self._safe_call(
                "daily_basic",
                trade_date=trade_date,
                fields="ts_code,trade_date,close,pe_ttm,pb,total_mv,circ_mv,dv_ttm,turnover_rate"
            )
            if not daily_df.empty:
                self.cache.put(db_key, daily_df)

        if stock_df.empty or daily_df.empty:
            return pd.DataFrame()

        # Merge on ts_code
        merged = stock_df.merge(daily_df, on="ts_code", how="inner")
        return merged

    # ---- Tier 1: Filter ----

    def _tier1_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply Tier 1 hard filters. Returns filtered DataFrame with 'channel' column."""
        if df.empty:
            df = df.copy()
            df["channel"] = pd.Series(dtype="object")
            return df

        cfg = self.config
        today = datetime.now()

        # 门槛 II（供 6c 的粗算 R 否决门用）
        # II = max(3.5%, Rf + 2%)，A股口径。缺失则跳过 R 门槛。
        self._tier1_ii = None
        if self._rf_cache is not None:
            self._tier1_ii = max(3.5, self._rf_cache + 2.0)
        else:
            try:
                _rf = self._cached_call("yc_cb", ts_code=None, curve_type="0")
                if not _rf.empty:
                    _rf = _rf.sort_values("trade_date", ascending=False)
                    self._rf_cache = float(_rf.iloc[0]["yield"])
                    self._tier1_ii = max(3.5, self._rf_cache + 2.0)
            except Exception:                          # noqa: BLE001
                pass

        # 1. Remove ST/PT/退市整理
        mask = ~df["name"].str.contains(r"\*ST|ST|PT|退市", na=False, regex=True)
        df = df[mask].copy()

        # 1b. Exclude banks unless include_bank is True
        #     行业名有两套体系，都要匹配：
        #       - 申万一级（新浪 stock_classify_sina）："银行"
        #       - 证监会门类（深交所/巨潮）："J 金融业" / "货币金融服务"
        #     只写 "银行" 会漏掉后者。
        if not cfg.include_bank:
            ind = df["industry"].fillna("").astype(str)
            BANK_RE = r"银行|货币金融"
            df = df[~ind.str.contains(BANK_RE, na=False, regex=True)].copy()

        # 2. Listing age >= min_listing_years
        #    list_date 空值（源缺失）不拦截，避免因数据缺失误杀
        cutoff = (today - timedelta(days=cfg.min_listing_years * 365)).strftime("%Y%m%d")
        ld = df["list_date"].fillna("").astype(str)
        df = df[(ld == "") | (ld <= cutoff)].copy()

        # 3. Market cap >= min_market_cap_yi (total_mv is in 万元, convert to 亿: / 10000)
        df = df[df["total_mv"].notna()].copy()
        df = df[df["total_mv"] / 10000 >= cfg.min_market_cap_yi].copy()

        # 4. Turnover >= min_turnover_pct
        df = df[df["turnover_rate"].notna()].copy()
        df = df[df["turnover_rate"] >= cfg.min_turnover_pct].copy()

        # 5. PB > 0 and <= max_pb
        df = df[df["pb"].notna()].copy()
        df = df[(df["pb"] > 0) & (df["pb"] <= cfg.max_pb)].copy()

        # 6. Dual-channel PE split (before dividend filter)
        #    亏损股（负 PE）→ 观察通道；盈利股 → main 通道。
        #    ⚠️ 源差异：Tushare 对亏损股返回 NaN，zzshare 返回【负 pe_ratio】。
        #       两者都必须纳入观察通道，否则 zzshare 下观察通道恒空。
        pe = df["pe_ttm"]
        loss_mask = pe.isna() | (pe < 0)
        main_mask = pe.notna() & (pe > 0) & (pe <= cfg.max_pe)
        obs_mask = loss_mask

        main_df = df[main_mask].copy()
        obs_df = df[obs_mask].copy()

        # 6b. 【Tier2 质量门槛前移】ROE / 毛利率 / 负债率
        #     这三项均为硬门槛，且可用全市场批量源一次拿到（~0.8s）。
        #     提前过滤可避免低质量股占用 Tier2 的 150 个名额。
        #     行业阈值（银行高杠杆）与 Tier2 保持一致。
        if cfg.tier1_apply_quality and not main_df.empty:
            try:
                fi = self._backend.fina_indicator_all()
            except Exception:                          # noqa: BLE001
                fi = None
            if fi is not None and len(fi):
                fi = fi.drop_duplicates("ts_code").set_index("ts_code")
                main_df["_ind"] = main_df["industry"].fillna("").astype(str)
                main_df["_bank"] = main_df["_ind"].apply(
                    lambda x: any(k in x for k in cfg.bank_industries))
                main_df["roe_waa"] = main_df["ts_code"].map(fi["roe_waa"])
                main_df["grossprofit_margin"] = main_df["ts_code"].map(fi["grossprofit_margin"])
                main_df["debt_to_assets"] = main_df["ts_code"].map(fi["debt_to_assets"])
                # 速动比率（智兔 cznl；银行整条缺记录 → NaN → 豁免）
                if "quick_ratio" in fi.columns:
                    main_df["quick_ratio"] = main_df["ts_code"].map(fi["quick_ratio"])
                else:
                    main_df["quick_ratio"] = None
                # 数据质量标记（roe 季度不全 / 离群值 / 无记录）
                if "_roe_flag" in fi.columns:
                    main_df["roe_flag"] = main_df["ts_code"].map(fi["_roe_flag"]).fillna("")
                    main_df["roe_quarters"] = main_df["ts_code"].map(fi["roe_quarters"])
                else:
                    main_df["roe_flag"] = ""
                    main_df["roe_quarters"] = None
                # 注：银行在 /hicw/cznl 中整条缺记录（实测 601398/601166 均无），
                # 此处不再逐股补值 —— 银行豁免阈值为 96%，实测负债率 91~93%
                # 本就通过，补值拦不住任何一只（纯浪费 40 次请求）。
                # 若报告需展示银行负债率，由 Tier2 的 /hs/fin/ratios 补齐。

                before = len(main_df)

                # ── ROE 门槛（2026-02 重写）───────────────────────────────
                # 旧写法 isna() | (>= min_roe) 把 NaN 当「免检放行」，
                # 导致「数据有问题的股反而不受门槛约束」。
                # 新语义分三种情况显式处理：
                #   1) 有值 且 >= min_roe      → 通过
                #   2) 有值 但 <  min_roe      → 剔除
                #   3) 无值（partial/outlier/无记录）→ 不剔除，但打标待核
                #      （不误杀：A 股合法停牌/次新股确有缺季；
                #        但不静默放行：roe_flag 会一路带到输出列）
                # 实测 2024 年度 zzshare 覆盖 5173/5173，其中 102 只季度
                # 不全（1 季 34 / 2 季 54 / 3 季 14），修复前这部分被
                # 部分相加后低调误杀。
                roe_v = pd.to_numeric(main_df["roe_waa"], errors="coerce")
                roe_has = roe_v.notna()
                ok = (~roe_has) | (roe_v >= cfg.min_roe)
                n_roe_drop = int((roe_has & (roe_v < cfg.min_roe)).sum())
                n_roe_na = int((~roe_has).sum())
                # 毛利率：银行为 0（不适用），其余 15%
                gm_min = main_df["_bank"].map(
                    lambda b: cfg.min_gross_margin_bank if b else cfg.min_gross_margin)
                ok &= main_df["grossprofit_margin"].isna() | \
                      (main_df["grossprofit_margin"] >= gm_min)
                # 负债率：批量源拿不到时（整列为空）不参与门控；
                # 若有值则过滤。
                debt_max = main_df["_bank"].map(
                    lambda b: cfg.max_debt_ratio_bank if b else cfg.max_debt_ratio)
                ok &= main_df["debt_to_assets"].isna() | \
                      (main_df["debt_to_assets"] <= debt_max)

                # ── 速动比率门槛（2026-02 新增）──────────────────────────
                # 来源：智兔 /hicw/cznl 的 sdbl（批量 5000 只，一次请求）。
                # 实测：神华 2.08 / 宇通 1.21 / 伊利 0.62。
                # 规则：sdbl < 0.8 → 直接筛除（无条件硬门槛）。
                # 豁免：金融股【跳过】—— 银行在 cznl 中整条缺记录（招行不在
                #       返回的 5000 只里），且存款即流动负债，速动比率本就不适用
                #       （与现有 min_gross_margin_bank=0 的处理一致）。
                # 缺失（非金融但拿不到值）→ 不剔除，与 ROE 的 NaN 语义一致：
                #       不因自身数据缺失而误杀，也不静默当成已通过。
                qr_v = pd.to_numeric(main_df["quick_ratio"], errors="coerce")
                _qr_checkable = (~main_df["_bank"]) & qr_v.notna()
                _qr_bad = _qr_checkable & (qr_v < cfg.min_quick_ratio)
                ok &= ~_qr_bad
                n_qr_drop = int(_qr_bad.sum())
                n_qr_na = int(((~main_df["_bank"]) & qr_v.isna()).sum())

                main_df = main_df[ok].drop(columns=["_ind", "_bank"])

                # 无数据股票单独提示（既不误杀也不误解为「已通过」）
                if n_roe_na:
                    flagged = main_df[main_df["roe_flag"] != ""]
                    by_flag = flagged["roe_flag"].value_counts().to_dict()
                    print(f"  [⚠️ ROE 数据待核] {n_roe_na} 只无有效年度 ROE"
                          f"（按标记: {by_flag}）—— 未剔除，已在输出列标注 roe_flag")
                if n_qr_drop or n_qr_na:
                    print(f"  [速动比率] 剔除 {n_qr_drop} 只（sdbl<{cfg.min_quick_ratio}）"
                          f" / {n_qr_na} 只无数据（非金融，未剔除——已标注）")
                if os.environ.get("SCREENER_VERBOSE") == "1":
                    print(f"  [Tier1 质量门槛前移] {before} -> {len(main_df)} 只"
                          f"（ROE>={cfg.min_roe}% 剔除 {n_roe_drop} 只"
                          f" / 毛利率>={cfg.min_gross_margin}%"
                          f" / 负债率<={cfg.max_debt_ratio}%"
                          f" / 速动比率>={cfg.min_quick_ratio} 剔除 {n_qr_drop} 只）")

        # 6b2. 观察通道：灌质量字段（ROE/毛利率/负债率/速动比率）
        #   亏损股不设 Tier1 硬门槛，字段留给 Tier2 _check_obs_quality 用。
        #   复用 fina_indicator_all()（内存缓存，0 额外请求）。
        if cfg.tier1_apply_quality and not obs_df.empty:
            try:
                fi = self._backend.fina_indicator_all()
            except Exception:                          # noqa: BLE001
                fi = None
            if fi is not None and len(fi):
                fi = fi.drop_duplicates("ts_code").set_index("ts_code")
                obs_df["roe_waa"] = obs_df["ts_code"].map(fi["roe_waa"])
                obs_df["grossprofit_margin"] = obs_df["ts_code"].map(fi["grossprofit_margin"])
                obs_df["debt_to_assets"] = obs_df["ts_code"].map(fi["debt_to_assets"])
                if "quick_ratio" in fi.columns:
                    obs_df["quick_ratio"] = obs_df["ts_code"].map(fi["quick_ratio"])
                else:
                    obs_df["quick_ratio"] = None
                if "_roe_flag" in fi.columns:
                    obs_df["roe_flag"] = obs_df["ts_code"].map(fi["_roe_flag"]).fillna("")
                    obs_df["roe_quarters"] = obs_df["ts_code"].map(fi["roe_quarters"])
                else:
                    obs_df["roe_flag"] = ""
                    obs_df["roe_quarters"] = None

        # 6c. 【框架因子2：M 计算（新口径）】（2026-02 重写）
        #
        # 为何重写：
        #   ① 旧 M 用智兔 yjbb 的【单年】fpfa，实测伊利 2025 漏计导致偏差
        #      26pp（批量 49.18% vs 逐股 75.48%）
        #   ② 单年口径 + 粗暴阈值会误杀「主动高分红」的公司（如宇通客车：
        #      放弃增量保证高分红，累计 M 103%）
        #
        # 新口径（三源均为全市场批量，5 年）：
        #   M_avg    = Σ5年分红 / Σ5年净利          （累计，天然抗噪）
        #   M_median = median( 正利润年 分红/利润 )   （同年比值，不缩尾）
        #   M_latest = 最新期 DPS / 最新期 EPS
        #   前置过滤：累计净利≤0 / 正利润年<3 → 排除
        #   M_final：最新年盈利 → max(M_avg, M_latest)
        #            最新年亏损 → min(M_avg, M_median)
        #   （OCF 质量门槛已删除 —— zzshare 的 OCF 数据错误，详见下方注释）
        #
        # 注：M 不参与 R 计算的截断（R 大本身是信号，后续筛查会继续过滤）。
        if cfg.tier1_apply_quality and not main_df.empty:
            M_cols = ["M_avg", "M_median", "M_latest", "M_final",
                      "M_flag", "M_reject", "div_5y", "np_5y"]
            # 先清理旧列（重跑幂等）
            main_df = main_df.drop(columns=[c for c in M_cols
                                            if c in main_df.columns],
                                   errors="ignore")
            try:
                d5 = self._backend.dividend_all_5y(
                    years=cfg.tier2_M_years)
            except Exception:                          # noqa: BLE001
                d5 = None
            if d5 is not None and len(d5):
                mm = self._compute_M_5y(main_df["ts_code"].tolist(), d5, cfg)
                M_KEYS = ("M_avg", "M_median", "M_latest", "M_final",
                          "M_flag", "M_reject", "div_5y", "np_5y")
                for c in M_KEYS:
                    main_df[c] = main_df["ts_code"].map(mm.get(c, {}))
                # ── 缓存供 Tier2 复用（2026-02）──
                #   Tier2 不再逐股重算 M，直接取此缓存（同口径同公式），
                #   避免两个 M 不一致，并省下逐股分红/利润表请求。
                #   注：M_reject 与 M_final 的 key 集合【互补】，两者并集才是全部。
                _all_codes = set(mm.get("M_final", {})) | set(mm.get("M_reject", {}))
                self._tier1_M_cache = {
                    code: {c: (mm.get(c) or {}).get(code) for c in M_KEYS}
                    for code in _all_codes
                }

                # 粗算 R(%) = 归母净利(5年均) × M(%) / 市值(百万元)
                #   ⚠️ 净利用 5 年均值（与 M_avg 同口径），避免用单年波动值
                mcap = main_df["total_mv"] * 10000 / 1e6      # 万元 -> 百万元
                main_df["net_profit"] = main_df["np_5y"]
                main_df["payout_ratio"] = main_df["M_final"]
                main_df["R_coarse"] = (
                    main_df["net_profit"] * (main_df["M_final"] / 100)
                    / mcap * 100).where(mcap > 0)

                before = len(main_df)
                # M 前置过滤（累计净利/正利润年）
                ok = main_df["M_reject"].isna()
                n_mrej = int((~ok).sum())

                # ⚠️ 【OCF 质量门槛已删除】(2026-02)
                # 原设计：M 高时校验 Σ5年OCF ≥ Σ5年分红。
                # 删除原因：zzshare finance_cash_flow 的 net_operate_cash_flow
                #   【数据错误】。实测宇通客车 600066：
                #     zzshare: 2024=26.75亿  2023=-4.60亿  2021=-0.06亿
                #     新浪  : 2024=72.11亿  2023=+47.17亿  2021=+7.16亿
                #   2023 年【连符号都相反】，与公开财报不符。
                # 而全市场批量源里无正确的 OCF 绝对额：
                #   智兔 /hicw/xj 只有比率（OCF/净利），无绝对值
                #   新浪 stock_financial_report_sina 准确但为【单股】接口
                # 故该判据无法在 Tier1 正确执行。若将来要恢复，需先找到
                #   批量且准确的 OCF 源，或将其移至 Tier2（逐股新浪）。

                # (b) 粗算 R 否决门：R >= II×0.5（缺失值放行）
                _ii = getattr(self, "_tier1_ii", None)
                if _ii is not None and cfg.tier1_apply_R:
                    ok &= main_df["R_coarse"].isna() | \
                          (main_df["R_coarse"] >= _ii * cfg.tier1_R_floor_mult)
                main_df = main_df[ok]

                if os.environ.get("SCREENER_VERBOSE") == "1":
                    _msg = (f"  [Tier1 M 前置过滤] {before} -> {len(main_df)} 只"
                            f"（净利/正利润年 剔除 {n_mrej} 只")
                    if _ii is not None and cfg.tier1_apply_R:
                        _msg += (f" / R>={_ii * cfg.tier1_R_floor_mult:.2f}%"
                                 f"=II×{cfg.tier1_R_floor_mult}")
                    print(_msg + "）")

        # 6d. 【质押硬否决】质押比例 > tier1_max_pledge 则剔除
        #     数据源：AkShare 全市场质押表（2294 行）。
        #     仅包含有质押的股票，未出现 = 无质押 = 放行。
        if cfg.tier1_apply_pledge and not main_df.empty:
            try:
                pl = self._backend.pledge_all()
            except Exception:                          # noqa: BLE001
                pl = None
            if pl is not None and len(pl):
                main_df["pledge_ratio"] = main_df["ts_code"].map(
                    pl.set_index("ts_code")["pledge_ratio"])
                before = len(main_df)
                ok = main_df["pledge_ratio"].isna() | \
                     (main_df["pledge_ratio"] <= cfg.tier1_max_pledge)
                main_df = main_df[ok]
                if os.environ.get("SCREENER_VERBOSE") == "1":
                    print(f"  [Tier1 质押否决] {before} -> {len(main_df)} 只"
                          f"（质押<={cfg.tier1_max_pledge}%）")

        # 7. Dividend yield > 0 — main channel only
        #    ⚠️ dv_ttm 实际为【历史年均股息率】(pergx)，非最近 12 个月。
        #       此项仅用于筛掉「从未分红」的公司（衡量分红历史/意愿）。
        #       详见 _fetch_zhitu_dividend 的 docstring。
        main_df = main_df[main_df["dv_ttm"].notna() & (main_df["dv_ttm"] > 0)].copy()
        main_df["channel"] = "main"

        # 8. Observation channel: top N by market cap (no dividend requirement)
        obs_df = obs_df.sort_values("total_mv", ascending=False).head(cfg.obs_channel_limit)
        obs_df["channel"] = "observation"

        result = pd.concat([main_df, obs_df], ignore_index=True)
        return result

    # ---- Tier 1: M 计算（2026-02 新口径）----

    def _compute_M_5y(self, codes: list, d5: pd.DataFrame,
                      cfg: "ScreenerConfig") -> dict:
        """批量计算 5 年口径的 M 三分量（Tier1 用，Tier2 复用）。

        输入 d5（backend.dividend_all_5y 的输出）：
          DataFrame[ts_code, year, dps, eps, div_total_mm, np_mm, payout, prog]

        输出：{列名: {ts_code: 值}}，列为：
          M_avg / M_median / M_latest / M_final / M_flag / M_reject
          div_5y / np_5y

        口径：
          M_avg    = Σ分红 / Σ净利
          M_median = median( 正利润年 分红/利润 )   ← 同年比值，不缩尾
          M_latest = 最新年 分红/利润
          前置过滤：累计净利≤0 / 正利润年<3
          M_final：最新年盈利 → max(M_avg, M_latest)
                   最新年亏损 → min(M_avg, M_median)
        """
        mwin = int(getattr(cfg, "tier2_M_years", 5) or 5)
        cols = ("M_avg", "M_median", "M_latest", "M_final",
                "M_flag", "M_reject", "div_5y", "np_5y")
        out: dict = {c: {} for c in cols}
        if d5 is None or not len(d5):
            return out
        sub = d5[d5["ts_code"].isin(set(codes))]
        if not len(sub):
            return out
        sub = sub.sort_values(["ts_code", "year"], ascending=[True, False])
        for code, g in sub.groupby("ts_code", sort=False):
            g = g.head(mwin)
            rows = []        # (year, np_mm, div_mm, payout)
            for _, r in g.iterrows():
                npmm = r.get("np_mm")
                if npmm is None or (isinstance(npmm, float) and npmm != npmm):
                    continue
                dvmm = r.get("div_total_mm")
                if dvmm is not None and isinstance(dvmm, float) and dvmm != dvmm:
                    dvmm = None
                py = r.get("payout")
                if py is not None and isinstance(py, float) and py != py:
                    py = None
                rows.append((int(r["year"]), float(npmm),
                             (float(dvmm) if dvmm is not None else None), py))
            if not rows:
                continue
            cum_np = sum(t[1] for t in rows)
            cum_dv = sum(t[2] for t in rows if t[2] is not None)
            pos = [t for t in rows if t[1] > 0]
            out["np_5y"][code] = cum_np
            out["div_5y"][code] = cum_dv
            # 前置过滤
            if cum_np <= 0:
                out["M_reject"][code] = "M_cum_neg"
                continue
            if len(pos) < 3:
                out["M_reject"][code] = "M_few_profit_years"
                continue
            if cum_dv == 0:
                M_avg = 0.0
            else:
                M_avg = cum_dv / cum_np * 100
            # 中位数（正利润年，同年比值，不缩尾）
            prs = sorted(t[3] for t in pos if t[3] is not None)
            M_median = None
            if prs:
                n = len(prs)
                M_median = (prs[n // 2] if n % 2
                            else (prs[n // 2 - 1] + prs[n // 2]) / 2)
            latest = rows[0]
            latest_np = latest[1]
            M_latest = latest[3]
            if latest_np > 0:
                cand = [v for v in (M_avg, M_latest) if v is not None]
                M_final = max(cand) if cand else None
            else:
                cand = [v for v in (M_avg, M_median) if v is not None]
                M_final = min(cand) if cand else None
            flags = []
            if M_latest is not None and M_latest > 100.0:
                flags.append("latest_gt100")
            if M_avg is not None and M_avg > 100.0:
                flags.append("avg_gt100_special_div")
            if M_final is not None and M_final > 100.0:
                flags.append("final_gt100")
            out["M_avg"][code] = M_avg
            out["M_median"][code] = M_median
            out["M_latest"][code] = M_latest
            out["M_final"][code] = M_final
            out["M_flag"][code] = ",".join(flags)
        return out

    # ---- Tier 1: Rank & Cut ----

    def _tier1_rank_and_cut(self, df: pd.DataFrame) -> pd.DataFrame:
        """Rank main channel by composite score, merge with observation channel."""
        if df.empty:
            return df

        cfg = self.config
        main = df[df["channel"] == "main"].copy()
        obs = df[df["channel"] == "observation"].copy()

        if not main.empty:
            # ── 排序：框架因子2 粗算 R 优先 ──
            # R_coarse 已是百分比，与门槛同量纲，无需归一化。
            if cfg.tier1_R_rank and "R_coarse" in main.columns \
                    and main["R_coarse"].notna().any():
                main["tier1_score"] = main["R_coarse"].fillna(-999)
                main = main.sort_values("tier1_score", ascending=False)
                if cfg.tier1_truncate:
                    main = main.head(cfg.tier2_main_limit)
                obs["tier1_score"] = 0.0
                return pd.concat([main, obs], ignore_index=True)

            # 回退：原三因子打分（股息率 / 1÷PE / 1÷PB）
            # Normalize for scoring: higher is better
            # dv_ttm: directly proportional
            # 1/PE: higher means cheaper
            # 1/PB: higher means cheaper
            dv_max = main["dv_ttm"].max()
            if dv_max > 0:
                main["_dv_norm"] = main["dv_ttm"] / dv_max
            else:
                main["_dv_norm"] = 0

            pe_inv = 1.0 / main["pe_ttm"]
            pe_inv_max = pe_inv.max()
            if pe_inv_max > 0:
                main["_pe_norm"] = pe_inv / pe_inv_max
            else:
                main["_pe_norm"] = 0

            pb_inv = 1.0 / main["pb"]
            pb_inv_max = pb_inv.max()
            if pb_inv_max > 0:
                main["_pb_norm"] = pb_inv / pb_inv_max
            else:
                main["_pb_norm"] = 0

            main["tier1_score"] = (
                cfg.dv_weight * main["_dv_norm"] +
                cfg.pe_weight * main["_pe_norm"] +
                cfg.pb_weight * main["_pb_norm"]
            )
            main = main.sort_values("tier1_score", ascending=False)
            if cfg.tier1_truncate:
                main = main.head(cfg.tier2_main_limit)
            main = main.drop(columns=["_dv_norm", "_pe_norm", "_pb_norm"])
        else:
            main["tier1_score"] = []

        # Observation channel: already limited in _tier1_filter
        obs["tier1_score"] = 0.0

        result = pd.concat([main, obs], ignore_index=True)
        return result

    # ---- Tier 2: Hard vetoes ----

    def _check_hard_vetoes(self, ts_code: str) -> tuple[bool, str]:
        """Check audit opinion.

        Returns (passed, reason). If passed=False, reason explains why.

        注：质押检查已【移至 Tier1】（见 _tier1_filter 第 6d 步）。
        Tier1 用 AkShare 全市场批量一次拿完，Tier2 再逐股查属纯冗余
        （且 run() 必经 Tier1，不存在跳过 Tier1 的单股入口）。
        """
        # Check audit opinion
        try:
            audit_df = self._cached_call("fina_audit", ts_code=ts_code)
            if not audit_df.empty:
                audit_df = audit_df.sort_values("end_date", ascending=False)
                result = audit_df.iloc[0].get("audit_result", "")
                if result and "标准无保留" not in str(result):
                    return False, f"non_standard_audit: {result}"
        except Exception:
            pass

        return True, ""

    def _check_debt_quality(self, ts_code: str) -> dict[str, Any]:
        """【已禁用，2026-02】原为「有息负债/净资产 ≤ 50%」质量门槛。

        🚫 为何禁用：有息负债科目【全链路拿不到】。实测 600887 伊利：
             - _cached_call("balancesheet") 实际返回列只有：
               money_cap / 交易性金融资产 / accounts_receiv / 存货 /
               contract_liab / acct_payable / 商誉 / 递延所得税资产 /
               递延所得税负债 / total_assets / total_liab / 总股本 等
               → 【st_borr / lt_borr / bond_payable 一个都没有】
             - datasource.get("资产负债表") 仅 13 字段，同样无借款
             - zzshare finance_balance 借款类【全空】(notna=0/5157)
             - AkShare stock_zcfz_em 无此列
           故该门槛对全部股票都是 'missing'，形同虚设。

        ✅ 已由 Tier1 的「资产负债率 ≤ 70%」(银行 96%) 覆盖同类风险，
           且后者数据完整（zzshare balance 100% 覆盖，含银行）。

        现恒返回 not-reject，调用方不会因此剔除任何股票。

        Returns:
            {interest_debt_to_equity: None,   # 永远取不到
             debt_quality_reject: False,
             debt_quality_flag: "disabled"}
        """
        return {"interest_debt_to_equity": None,
                "debt_quality_reject": False,
                "debt_quality_flag": "disabled"}

    def _check_allocation_capacity(self, ts_code: str) -> tuple[bool, dict[str, Any]]:
        """框架因子2【分配能力验证】—— 一票否决项。

        框架原文：
          逐年列出过去 3-5 年的 经营CF / 投资CF / 自由现金流 / 融资CF / 期末现金，
          判断：
            ① 自由现金流过去 3-5 年是否持续为正？
            ② 融资 CF 是否持续为正（持续借债）？
            ③ 派息后现金储备是否仍在净增长？
          否决规则：
            「若自由现金流长期为负 且 融资活动持续为正
              → 判定为伪穿透回报 → 一票否决」

        实现口径（保守，避免误杀）：
          - 窗口：可得的年报，最多 5 年
          - FCF = 经营活动现金流 - |资本开支|
          - 「长期为负」：窗口内【半数以上】年份 FCF < 0
          - 「持续为正」：窗口内【半数以上】年份融资 CF > 0
          - 两者同时成立 → 否决

        Returns (passed, metrics)。数据不足时【放行】（不误杀）。
        """
        metrics: dict[str, Any] = {}
        try:
            cf_df = self._cached_call("cashflow", ts_code=ts_code)
        except Exception:                              # noqa: BLE001
            return True, metrics
        if cf_df is None or cf_df.empty:
            return True, metrics

        cf_df = cf_df.sort_values("end_date", ascending=False)
        annual = cf_df[cf_df["end_date"].astype(str).str.endswith("1231")]
        # 只取已完成的年度
        try:
            import datetime as _dtc
            cur_year = _dtc.date.today().year
            annual = annual[annual["end_date"].astype(str).str[:4].astype(int) < cur_year]
        except Exception:                              # noqa: BLE001
            pass
        if annual.empty or len(annual) < 3:
            # 数据不足 3 年 —— 不做判断（避免误杀）
            metrics["alloc_years"] = len(annual)
            return True, metrics

        rows = annual.head(5)
        fcf_neg = 0
        fin_pos = 0
        n = 0
        detail = []
        for _, r in rows.iterrows():
            ocf = _sf(r.get("n_cashflow_act"))
            if ocf is None:
                ocf = _sf(r.get("经营活动现金流"))
            capex_raw = _sf(r.get("c_pay_acq_const_fiolta"))
            if capex_raw is None:
                capex_raw = _sf(r.get("资本开支"))
            fin = _sf(r.get("n_finance_cash_flow"))
            if fin is None:
                fin = _sf(r.get("融资活动现金流"))
            if ocf is None:
                continue
            capex = abs(capex_raw or 0.0)
            fcf = ocf - capex
            n += 1
            if fcf < 0:
                fcf_neg += 1
            if fin is not None and fin > 0:
                fin_pos += 1
            detail.append({
                "year": str(r["end_date"])[:4],
                "fcf": round(fcf / 1e6, 0),
                "fin_cf": round((fin or 0.0) / 1e6, 0),
            })

        if n < 3:
            metrics["alloc_years"] = n
            return True, metrics

        half = n / 2.0
        fcf_persist_neg = fcf_neg > half        # 半数以上年份 FCF < 0
        fin_persist_pos = fin_pos > half        # 半数以上年份融资 CF > 0

        metrics["alloc_years"] = n
        metrics["alloc_fcf_neg_years"] = fcf_neg
        metrics["alloc_fin_pos_years"] = fin_pos
        metrics["alloc_fcf_persist_neg"] = fcf_persist_neg
        metrics["alloc_fin_persist_pos"] = fin_persist_pos
        metrics["alloc_detail"] = detail

        if fcf_persist_neg and fin_persist_pos:
            return False, metrics
        return True, metrics

    # ---- Tier 2: Financial quality ----

    def _industry_of(self, ts_code: str) -> str:
        """取标的行业名（优先快照的申万行业；缺失则查智兔）。"""
        try:
            snap = self._backend.snapshot()
            hit = snap[snap["ts_code"] == ts_code]
            if len(hit):
                ind = str(hit.iloc[0].get("industry") or "")
                if ind:
                    return ind
        except Exception:                              # noqa: BLE001
            pass
        return ""

    def _check_financial_quality(self, ts_code: str, channel: str = "main"
                                 ) -> tuple[bool, dict[str, Any]]:
        """Check ROE, gross margin, debt ratio. For observation channel, also check profit_dedt.

        Returns (passed, metrics_dict).
        """
        cfg = self.config
        metrics: dict[str, Any] = {}

        try:
            fi_df = self._cached_call("fina_indicator", ts_code=ts_code)
        except Exception:
            return False, metrics

        if fi_df.empty:
            return False, metrics

        # Use latest 【completed】 annual report（排除未完成年度）
        fi_df = fi_df.sort_values("end_date", ascending=False)
        import datetime as _dtc
        cur_year = _dtc.date.today().year
        annual = fi_df[(fi_df["end_date"].str.endswith("1231")) &
                       (fi_df["end_date"].str[:4].astype(int) < cur_year)]
        if annual.empty:
            # 无已完成年报 -> 用最新一期（可能是中报），但标注
            fi_df2 = fi_df[fi_df["end_date"].str[:4].astype(int) < cur_year]
            if fi_df2.empty:
                return False, metrics
            row = fi_df2.iloc[0]
        else:
            row = annual.iloc[0]

        roe = row.get("roe_waa")
        gm = row.get("grossprofit_margin")
        debt = row.get("debt_to_assets")
        profit_dedt = row.get("profit_dedt")

        # 免费源对部分股票（尤银行）给不出毛利率/资产负债率（值为 None），
        # 且 grossprofit_margin 存在明显失真（实测茅台 365%、沪农商行
        # 净利率 194%，而智兔分别为 91%/53%、49%）——只要出现 None
        # 或 >100%（毛利率不可能超 100%）就用智兔 /hs/fin/ratios/ 覆盖：
        #   zcfzl=资产负债率、mlv=毛利率、jqjzcsyl=加权ROE
        _gm_bad = (gm is None) or (gm != gm) or (gm > 100) or (gm < -100)
        _debt_bad = (debt is None) or (debt != debt)
        if _gm_bad or _debt_bad:
            try:
                zt = self._backend._zt_fin_ratios(ts_code)
                if zt:
                    if _gm_bad and zt.get("grossprofit_margin") is not None:
                        gm = zt["grossprofit_margin"]
                    if _debt_bad and zt.get("debt_to_assets") is not None:
                        debt = zt["debt_to_assets"]
                    if (roe is None or roe != roe) and zt.get("roe_waa") is not None:
                        roe = zt["roe_waa"]
            except Exception:                          # noqa: BLE001
                pass

        metrics["roe_waa"] = float(roe) if roe is not None and roe == roe else None
        metrics["gross_margin"] = float(gm) if gm is not None and gm == gm else None
        metrics["debt_to_assets"] = float(debt) if debt is not None and debt == debt else None
        metrics["profit_dedt"] = float(profit_dedt) if profit_dedt is not None and profit_dedt == profit_dedt else None

        # Observation channel: FCF-based quality gates
        if channel == "observation":
            return self._check_obs_quality(ts_code, metrics, cfg)

        # Financial quality checks (main channel)
        # ⚠️ 已前移至 Tier1（见 screener_config.tier1_apply_quality）：
        #    ROE/毛利率/负债率 三项均用【智兔 /hicw/* 全市场批量】筛过一遍，
        #    Tier2 再判一次属重复（150 只 × 逐股请求，纯浪费）。
        #    此处【只采集指标供打分/报告用】，不再做门槛判断。
        #    用 tier2_recheck_quality=True 可恢复旧行为（调试用）。
        ind = self._industry_of(ts_code)
        is_bank = any(k in ind for k in cfg.bank_industries)
        max_debt = cfg.max_debt_ratio_bank if is_bank else cfg.max_debt_ratio
        min_gm = cfg.min_gross_margin_bank if is_bank else cfg.min_gross_margin
        metrics["industry"] = ind
        metrics["is_bank"] = is_bank
        metrics["max_debt_used"] = max_debt
        metrics["min_gm_used"] = min_gm

        if getattr(cfg, "tier2_recheck_quality", False):
            if metrics["roe_waa"] is not None and metrics["roe_waa"] < cfg.min_roe:
                return False, metrics
            if metrics["gross_margin"] is not None and metrics["gross_margin"] < min_gm:
                return False, metrics
            if metrics["debt_to_assets"] is not None and metrics["debt_to_assets"] > max_debt:
                return False, metrics

        # ── Tier2 细筛：新浪年报口径 ROE ──────────────────────────────
        # 注意：本函数（_check_financial_quality）在当前 Tier2 流程中已是
        # 【死代码】—— 质量门槛已前移至 Tier1，实际细筛在
        # TushareScreener._check_one 的 "Step 2c" 中执行（见本文件）。
        # 此处保留仅作参考/调试（tier2_recheck_quality=True 时生效），
        # 逻辑与主路径保持一致：无值不剔除但标注，低于门槛则剔除。
        metrics["roe_refined"] = None
        metrics["roe_refined_end"] = ""
        metrics["roe_refined_flag"] = ""
        if getattr(cfg, "refined_roe_apply", True):
            try:
                rf = self._backend.roe_refined(ts_code)
            except Exception:                          # noqa: BLE001
                rf = None
            if rf:
                metrics["roe_refined"] = rf.get("roe_refined")
                metrics["roe_refined_end"] = rf.get("end_date", "")
                metrics["roe_refined_flag"] = rf.get("refined_flag", "")
            rv = metrics["roe_refined"]
            if rv is None:
                metrics["roe_refined_flag"] = \
                    metrics["roe_refined_flag"] or "missing"
            elif rv < cfg.min_roe_refined:
                metrics["roe_refined_reject"] = True
                return False, metrics

        return True, metrics

    def _check_obs_quality(self, ts_code: str, metrics: dict[str, Any],
                           cfg: "ScreenerConfig") -> tuple[bool, dict[str, Any]]:
        """Observation-channel quality check: FCF-based signals.

        Gates:
        1. ROE >= min_roe_obs (default -999, 等效禁用：亏损股 ROE 天然为负)
        2. Gross margin >= min_gross_margin (same as main, 15%)
        3. Debt ratio <= max_debt_ratio (same as main, 70%)
        4. OCF > 0 (if obs_require_ocf_positive)
        5. FCF margin (FCF/Revenue) >= min_fcf_margin_obs (default 0%)
        6. FCF positive years >= min_fcf_positive_years_obs (default 2 of 5)
        """
        # Gate 1: ROE 门槛（默认 -999 等效禁用）
        #   观察通道 = 亏损股，ROE 天然为负，无法设正下限；此门槛仅用于
        #   拦截极端数据异常（roe_waa 已在上游按 |ROE|>200% 置 None）。
        if metrics["roe_waa"] is not None and metrics["roe_waa"] < cfg.min_roe_obs:
            return False, metrics

        # Gate 2: Gross margin (same as main)
        if metrics["gross_margin"] is not None and metrics["gross_margin"] < cfg.min_gross_margin:
            return False, metrics

        # Gate 3: Debt ratio (same as main)
        if metrics["debt_to_assets"] is not None and metrics["debt_to_assets"] > cfg.max_debt_ratio:
            return False, metrics

        # Fetch cashflow and income for FCF-based gates
        # (cached: will be reused by Factor 2/4 later, zero extra API calls)
        try:
            cf_df = self._cached_call("cashflow", ts_code=ts_code)
            income_df = self._cached_call("income", ts_code=ts_code,
                                          report_type="1")
        except Exception:
            return False, metrics

        if cf_df.empty:
            return False, metrics

        def _sf(val):
            if val is None:
                return None
            try:
                f = float(val)
                return None if f != f else f
            except (TypeError, ValueError):
                return None

        cf_sorted = cf_df.sort_values("end_date", ascending=False)
        annual_cf = cf_sorted[cf_sorted["end_date"].str.endswith("1231")]

        if annual_cf.empty:
            return False, metrics

        latest_cf = annual_cf.iloc[0]
        ocf = _sf(latest_cf.get("n_cashflow_act"))
        capex = _sf(latest_cf.get("c_pay_acq_const_fiolta"))

        # Gate 4: OCF positive
        if cfg.obs_require_ocf_positive:
            if ocf is None or ocf <= 0:
                return False, metrics

        # Compute FCF (raw yuan)
        ocf_val = ocf if ocf is not None else 0
        capex_val = abs(capex) if capex is not None else 0
        fcf = ocf_val - capex_val

        # Gate 5: FCF margin = FCF / Revenue * 100 (both in raw yuan)
        revenue = None
        if not income_df.empty:
            inc_sorted = income_df.sort_values("end_date", ascending=False)
            annual_inc = inc_sorted[inc_sorted["end_date"].str.endswith("1231")]
            if not annual_inc.empty:
                revenue = _sf(annual_inc.iloc[0].get("revenue"))

        if revenue is not None and revenue > 0:
            fcf_margin = fcf / revenue * 100
            metrics["fcf_margin"] = fcf_margin
            if fcf_margin < cfg.min_fcf_margin_obs:
                return False, metrics
        else:
            return False, metrics

        # Gate 6: FCF consistency (positive years out of 5)
        fcf_list = []
        for _, r in annual_cf.head(5).iterrows():
            o = _sf(r.get("n_cashflow_act"))
            c = _sf(r.get("c_pay_acq_const_fiolta"))
            if o is not None and c is not None:
                fcf_list.append(o - abs(c))

        if fcf_list:
            positive_years = sum(1 for f in fcf_list if f > 0)
            metrics["fcf_positive_years"] = positive_years
            if positive_years < cfg.min_fcf_positive_years_obs:
                return False, metrics

        return True, metrics

    # ---- Tier 2: Factor 2 penetration return ----

    def _extract_factor2_metrics(self, ts_code: str, total_mv_wan: float
                                 ) -> dict[str, Any]:
        """Extract Factor 2 metrics: payout ratio M, penetration return R, threshold II.

        R = AA × M / 市值, where AA = 真实可支配现金结余 ≈ OCF + V1 - V_deduct - |Capex|.

        Args:
            ts_code: Stock code.
            total_mv_wan: Total market value in 万元.

        Returns:
            Dict with keys: AA, M, R, II, Rf, R_vs_II (pass/marginal/fail/below_rf).
        """
        result: dict[str, Any] = {}
        cfg = self.config
        mkt_cap = total_mv_wan * 10000 / 1e6  # 百万元

        # Get risk-free rate (cached globally)
        if self._rf_cache is None:
            try:
                rf_df = self._cached_call("yc_cb", ts_code=None, curve_type="0")
                if not rf_df.empty:
                    rf_df = rf_df.sort_values("trade_date", ascending=False)
                    self._rf_cache = float(rf_df.iloc[0]["yield"])
            except Exception:
                pass

        rf = self._rf_cache
        result["Rf"] = rf

        if rf is not None:
            ii = max(3.5, rf + 2.0)
            result["II"] = ii
        else:
            result["II"] = None

        # Get income, dividend, and cashflow data
        try:
            income_df = self._cached_call("income", ts_code=ts_code,
                                          report_type="1")
            div_df = self._cached_call("dividend", ts_code=ts_code)
        except Exception:
            return result

        try:
            cf_df = self._cached_call("cashflow", ts_code=ts_code)
        except Exception:
            cf_df = pd.DataFrame()

        if income_df.empty:
            return result

        income_df = income_df.sort_values("end_date", ascending=False)
        annual_inc = income_df[income_df["end_date"].str.endswith("1231")]

        # 分红数据：按年汇总（同一年多条分红记录需【累加】，
        # 如伊利 2025 有中期 0.9 + 年度 0.48；且只计已实施/预案，
        # 跳过「取消/股东大会否决」）。
        div_lookup: dict[str, float] = {}
        if not div_df.empty:
            div_df = div_df.sort_values("end_date", ascending=False)
            for _, r in div_df.iterrows():
                y = str(r.get("end_date", ""))[:4]
                if not y:
                    continue
                proc = str(r.get("div_proc", "") or "")
                if proc and ("实施" not in proc and "预案" not in proc):
                    continue
                cash_div = r.get("cash_div_tax")
                base_share = r.get("base_share")
                if (cash_div is not None and pd.notna(cash_div)
                        and base_share is not None and pd.notna(base_share)):
                    try:
                        div_total = (float(cash_div) * float(base_share)
                                     * 10000 / 1e6)          # 百万元
                        div_lookup[y] = div_lookup.get(y, 0.0) + div_total
                    except (ValueError, TypeError):
                        pass

        # ── 支付率锚定值 M（框架因子2 步骤5，2026-02 重写）────────────
        #
        # 旧实现的缺陷：
        #   ① M_avg = 对【单年】分红/净利 取 5 年平均
        #      → 净利塌陷年会产生 800% 之类的爆炸值，直接污染均值
        #   ② M = max(M_avg, M_latest) 无条件取大
        #      → 最新年亏损时 M_latest 不可信，却仍能抬高 M
        #   ③ 无任何长期可持续性检查
        #
        # 新口径（分层，按【持续性】而非【单点】判定）：
        #   前置过滤：累计净利≤0 / 正利润年<3  → 排除
        #   M_avg    = 累计分红 / 累计净利（累计口径天然抗噪）
        #   M_median = 正利润年缩尾后支付率的中位数
        #   M_latest = 最新期 DPS / 最新期 EPS
        #
        #   分量检查（无条件，先于 M_final）：
        #     M_avg    > 120% → 某年异常到 200%+，分母塌陷/数据异常 → 排除
        #     M_avg    > 100% → 某年特别分红 → 保留 + 标记 avg_gt100_special_div
        #     M_median > 100% → 过半正常年不可持续         → 排除
        #   最终取值：
        #     最新年盈利 → M = max(M_avg, M_latest)   允许反映「最近真的多分了」
        #     最新年亏损 → M = min(M_avg, M_median)   最新信号不可信，双保险取小
        #   标记（不拦截）：
        #     M_latest > 100% → M_flag='latest_gt100'（仅 1 年，样本太小，不定罪）
        #
        # 原则：Tier2 是最后一道防线，宁可保守，不可乐观。
        #
        # ⚠️ 【2026-02 改为复用 Tier1 的 M】(不再逐股重算)
        #   理由：Tier1 已改用 AkShare stock_fhps_em（5 年批量）算 M，
        #         数据源与口径均已统一，无需在 Tier2 重算（避免两个 M 不一致）。
        #   Tier1 传入在 _analyze_single 的 row 中（M_avg / M_median /
        #   M_latest / M_final / M_flag / M_reject / div_5y）。
        #   只有 Tier1 未传出时（如单独调试单股）才回退到逐股计算。
        M_avg = M_median = M_latest = M_final = None
        M_flag = ""
        M_reject = None
        M_diag: dict = {}
        _mwin = int(getattr(cfg, "tier2_M_years", 5) or 5)
        _w_lo, _w_hi = 0.0, 150.0
        yearly: list[tuple[str, float, float | None, float | None]] = []
        if not annual_inc.empty:
            _years = [str(r["end_date"])[:4]
                      for _, r in annual_inc.head(_mwin).iterrows()]
            for y in _years:
                _np_f = None
                for _, r in annual_inc.iterrows():
                    if str(r["end_date"])[:4] == y:
                        v = r.get("n_income_attr_p")
                        if v is not None and pd.notna(v):
                            try:
                                _np_f = float(v) / 1e6        # 百万元
                            except (ValueError, TypeError):
                                _np_f = None
                        break
                if _np_f is None:
                    continue
                _dv_f = div_lookup.get(y)                     # 百万元，可能 None
                _pr_y = None
                if _dv_f is not None and _np_f > 0:
                    _pr_y = _dv_f / _np_f * 100
                yearly.append((y, _np_f, _dv_f, _pr_y))

        # （旧逐股 M 计算已整体移除 —— 现【复用 Tier1 的 stock_fhps_em
        #   结果】，见下方「M 取值」区块。去掉重算既避免两个 M 不一致，
        #   也省下逐股分红/利润表请求。）
        _ = yearly

        # ⚠️ M 取值（2026-02）：优先复用 Tier1 的批量结果。
        #   Tier1 的 stock_fhps_em 与 Tier2 原逐股路径【同口径同公式】，
        #   复用可避免两个 M 不一致（同时省下逐股分红/利润表请求）。
        #   M_refined 等旧列名保留以兼容已有消费方（R_refined 等）。
        _pre = self._tier1_M_cache.get(ts_code) if hasattr(self, "_tier1_M_cache") else None
        if _pre:
            M_avg = _pre.get("M_avg")
            M_median = _pre.get("M_median")
            M_latest = _pre.get("M_latest")
            M_final = _pre.get("M_final")
            M_flag = _pre.get("M_flag") or ""
            M_reject = _pre.get("M_reject")
            M_diag = {"source": "tier1_fhps_em",
                      "div_5y": _pre.get("div_5y"),
                      "np_5y": _pre.get("np_5y")}
        else:
            # 回退：逐股计算（仅当 Tier1 未传值时，如单独调试单股）
            M_diag["source"] = "tier2_local"
        result["M_source"] = M_diag.get("source", "")
        result["M"] = M_final
        result["M_refined"] = M_final
        result["M_refined_avg"] = M_avg
        result["M_refined_median"] = M_median
        result["M_refined_latest"] = M_latest
        result["M_refined_window"] = _mwin
        result["M_refined_flag"] = M_flag
        result["M_refined_reject"] = M_reject
        result["M_refined_diag"] = M_diag
        result["M_avg3"] = M_avg
        result["M_latest"] = M_latest

        # AA = 真实可支配现金结余（对齐 §17.5 完整 11 步口径）
        #
        # 旧实现（已废弃）：AA = OCF - V_deduct - Capex
        #   缺陷：① 起点是 OCF（结果），非从营收还原
        #         ② V1（资产处置）硬编码 0，从不取值
         #         ③ 多扣 V_deduct（§17.5 无此扣减）→ 系统性低估 AA
        #
        # 新实现：AA = 真实现金收入 + V1 + V5 - W - Y
        #   真实现金收入 = S - AR净增 - max(预收净减, 0)
        #   W = W1(营业成本+应付净减) + W2(员工) + W3(税) + W4(利息)
        #   Y = Capex + 取得子公司支付
        #   V1 = 处置固定资产收回；V5 = 取得投资收益收到
        AA = None
        aa_detail: dict[str, float] = {}
        try:
            aa_win = int(os.environ.get("SCREENER_AA_YEARS", "2"))
        except ValueError:
            aa_win = 2
        # ⚠️ 必须在 if 块【外】初始化：cf_df/annual_inc 任一为空时下面整块跳过，
        #    但 L1209 仍会读 aa_vals（AA_years 统计），否则 UnboundLocalError。
        aa_vals: list[float] = []
        if not cf_df.empty and not annual_inc.empty:
            cf_sorted = cf_df.sort_values("end_date", ascending=False)
            annual_cf = cf_sorted[cf_sorted["end_date"].astype(str).str.endswith("1231")]
            import datetime as _dtc
            annual_cf = annual_cf[annual_cf["end_date"].astype(str).str[:4].astype(int)
                                  < _dtc.date.today().year]

            # 资产负债表（应收/应付/合同负债）—— 与现金流同期
            try:
                bs_df = self._cached_call("balancesheet", ts_code=ts_code)
                bs_df = bs_df.sort_values("end_date", ascending=False) \
                    if not bs_df.empty else bs_df
            except Exception:                          # noqa: BLE001
                bs_df = pd.DataFrame()

            def _year_row(df, y):
                if df is None or df.empty:
                    return None
                hit = df[df["end_date"].astype(str).str[:4] == y]
                return hit.iloc[0] if len(hit) else None

            _degraded_ar = []          # 记录缺前年 AR/预收 的年份
            for _, cf_row in annual_cf.head(aa_win).iterrows():
                yr = str(cf_row["end_date"])[:4]
                inc_row = _year_row(annual_inc, yr)
                bs_row = _year_row(bs_df, yr)
                # 上一年的资产负债表——用于算 AR/预收/应付 的【净变动】。
                # ⚠️ 拿不到就是拿不到，【不得回退到当年】（否则净变动恒为 0，
                #    相当于没做 §17.3 的现金收入还原）。
                bs_prev = _year_row(bs_df, str(int(yr) - 1))
                if inc_row is None or cf_row is None:
                    continue

                # ---- 真实现金收入（§17.3）----
                S = _sf(inc_row.get("revenue")) or _sf(inc_row.get("营业收入"))
                if S is None:
                    continue

                def _pick(d, *names):
                    """按优先级取第一个非空值（兼容英文 Tushare 名 / 中文免费源名）。"""
                    if d is None:
                        return None
                    for n in names:
                        v = _sf(d.get(n))
                        if v is not None:
                            return v
                    return None

                T = 0.0    # AR 净增（扣除）
                U = 0.0    # 预收净减（扣除）
                _ar_adj_ok = True
                ar_now = _pick(bs_row, "accounts_receiv", "应收账款")
                ar_prev = _pick(bs_prev, "accounts_receiv", "应收账款")
                cl_now = _pick(bs_row, "contract_liab", "合同负债")
                cl_prev = _pick(bs_prev, "contract_liab", "合同负债")
                if ar_now is not None and ar_prev is not None:
                    T = ar_now - ar_prev
                else:
                    _ar_adj_ok = False
                if cl_now is not None and cl_prev is not None:
                    U = min(cl_now - cl_prev, 0.0)       # 仅净减扣，净增不加回
                else:
                    _ar_adj_ok = False
                if not _ar_adj_ok:
                    _degraded_ar.append(yr)
                cash_rev = S - T - abs(U)

                # ---- 经营性现金支出 W（§17.4）----
                # 注意：dataset_call 输出可能用中文列名（如「支付给职工现金」），
                #       也可能用已回译的 Tushare 名，两种都要试。
                W1 = _pick(inc_row, "oper_cost", "营业成本") or 0.0
                ap_now = _pick(bs_row, "acct_payable", "应付账款")
                ap_prev = _pick(bs_prev, "acct_payable", "应付账款")
                if ap_now is not None and ap_prev is not None:
                    W1 += (ap_prev - ap_now)              # 应付净减 -> 多付现金
                W2 = _pick(cf_row, "支付给职工现金", "c_pay_to_staff", "c_paid_to_for_empl") or 0.0
                W3 = _pick(inc_row, "所得税", "income_tax") or 0.0
                # W4 利息：
                # ⚠️ 不能用「分配股利偿付利息」（120 亿，含股利，严重偏大）。
                # ⚠️ 也不能用财务费用【绝对值】——伊利财务费用为负（利息净收入），
                #    取绝对值会凭空造出一笔利息支出。
                # 取【财务费用净额】本身（可正可负），与营业成本同属「经营支出」项；
                # 若为负（净利息收入），相当于减少支出，是真实口径。
                W4 = _pick(inc_row, "财务费用", "finance_exp", "fin_exp") or 0.0
                W = W1 + W2 + W3 + W4

                # ---- 总扣除 Y ----
                capex = abs(_pick(cf_row, "资本开支", "c_pay_acq_const_fiolta") or 0.0)
                Y = capex

                # ---- 非经常性流入 V1 / V5 ----
                v1 = _pick(cf_row, "处置固定资产收回", "n_recp_disp_fiolta", "c_disp_fiolta_recp") or 0.0
                v5 = _pick(cf_row, "取得投资收益收到", "n_recp_inv_income", "c_recp_return_invest") or 0.0

                # ---- 字段完整度保护 ----
                # 免费后端的关键支出字段大面积缺失（实测伊利）：
                #   c_pay_to_staff（员工 W2）、c_pay_acq_const_fiolta（Capex）、
                #   accounts_receiv（AR）、contract_liab（合同负债）→ 均 None
                # 此时 W 只剩「营业成本±应付」，会【低估支出、高估 AA】。
                # 故只要缺任一关键项，就改用 OCF 作为支出总额的【上限】约束：
                #   AA ≈ min(真实现金收入, OCF) + V1 + V5 - Capex
                # 依据：OCF = 真实现金收入 - W（近似），用 min 截住不会虚高。
                _miss_w2 = _pick(cf_row, "支付给职工现金", "c_pay_to_staff") is None
                _miss_capex = _pick(cf_row, "资本开支", "c_pay_acq_const_fiolta") is None
                _miss_ar = (ar_now is None or ar_prev is None)
                _miss_cl = (cl_now is None or cl_prev is None)
                if _miss_w2 or _miss_capex or _miss_ar or _miss_cl:
                    ocf = _pick(cf_row, "n_cashflow_act", "经营活动现金流")
                    if ocf is not None:
                        bal = min(cash_rev, ocf) + v1 + v5 - Y
                        caliber = "§17.5(OCF护)"
                    else:
                        bal = cash_rev + v1 + v5 - W - Y
                        caliber = "§17.5(缺字段)"
                else:
                    bal = cash_rev + v1 + v5 - W - Y
                    caliber = "§17.5(11步)"

                aa_vals.append(bal / 1e6)                 # 百万元
                aa_detail[yr] = bal / 1e6
                result["AA_caliber"] = caliber
            if _degraded_ar:
                result["AA_degraded_years"] = _degraded_ar
            if aa_vals:
                AA = sum(aa_vals) / len(aa_vals)

        result["AA"] = AA
        result["AA_years"] = len(aa_vals) if aa_vals else 0
        result["AA_detail"] = aa_detail
        result.setdefault("AA_caliber", None)

        # R = AA × M / mkt_cap  (O=0, 无法区分回购用途)
        if AA is not None and result.get("M") is not None and mkt_cap > 0:
            R = AA * (result["M"] / 100) / mkt_cap * 100  # percentage
            result["R"] = R
        else:
            result["R"] = None

        # Classify R vs II
        if result.get("R") is not None and result.get("II") is not None and result.get("Rf") is not None:
            R = result["R"]
            II = result["II"]
            Rf = result["Rf"]
            if R < Rf:
                result["R_vs_II"] = "below_rf"
            elif R < II * 0.5:
                result["R_vs_II"] = "fail"
            elif R < II:
                result["R_vs_II"] = "marginal"
            else:
                result["R_vs_II"] = "pass"
        else:
            result["R_vs_II"] = None

        return result

    # ---- Tier 2: Factor 4 valuation metrics ----

    def _extract_factor4_metrics(self, ts_code: str, close: float,
                                 total_mv_wan: float) -> dict[str, Any]:
        """Extract Factor 4 metrics: EV/EBITDA, cash-adj PE, FCF yield, etc.

        Args:
            ts_code: Stock code.
            close: Current stock price.
            total_mv_wan: Total market value in 万元.
        """
        result: dict[str, Any] = {}
        mkt_cap = total_mv_wan * 10000 / 1e6  # 百万元

        try:
            income_df = self._cached_call("income", ts_code=ts_code,
                                          report_type="1")
            bs_df = self._cached_call("balancesheet", ts_code=ts_code,
                                      report_type="1")
            cf_df = self._cached_call("cashflow", ts_code=ts_code)
        except Exception:
            return result

        def _sf(val):
            if val is None:
                return None
            try:
                f = float(val)
                return None if f != f else f
            except (TypeError, ValueError):
                return None

        # Try pre-computed values from fina_indicator (already cached)
        fi_ebitda = fi_netdebt = fi_fcff = None
        try:
            fi_df = self._cached_call("fina_indicator", ts_code=ts_code)
            if not fi_df.empty:
                fi_sorted = fi_df.sort_values("end_date", ascending=False)
                annual_fi = fi_sorted[fi_sorted["end_date"].str.endswith("1231")]
                if not annual_fi.empty:
                    fi_row = annual_fi.iloc[0]
                    fi_ebitda = _sf(fi_row.get("ebitda"))
                    fi_netdebt = _sf(fi_row.get("netdebt"))
                    fi_fcff = _sf(fi_row.get("fcff"))
        except Exception:
            pass

        # Process income
        if not income_df.empty:
            income_df = income_df.sort_values("end_date", ascending=False)
            annual_inc = income_df[income_df["end_date"].str.endswith("1231")]
            if not annual_inc.empty:
                latest = annual_inc.iloc[0]
                oper_profit = _sf(latest.get("operate_profit"))
                fin_exp = _sf(latest.get("finance_exp"))
                np_parent = _sf(latest.get("n_income_attr_p"))

                rev_raw = _sf(latest.get("revenue"))

                if oper_profit is not None:
                    result["oper_profit"] = oper_profit / 1e6
                if np_parent is not None:
                    result["np_parent"] = np_parent / 1e6
                if rev_raw is not None and rev_raw > 0:
                    result["revenue"] = rev_raw / 1e6  # 百万元

        # Process balance sheet
        if not bs_df.empty:
            bs_df = bs_df.sort_values("end_date", ascending=False)
            annual_bs = bs_df[bs_df["end_date"].str.endswith("1231")]
            if not annual_bs.empty:
                latest = annual_bs.iloc[0]
                cash = (_sf(latest.get("money_cap")) or 0) / 1e6
                trad = (_sf(latest.get("trad_asset")) or 0) / 1e6
                goodwill = (_sf(latest.get("goodwill")) or 0) / 1e6
                ta = (_sf(latest.get("total_assets")) or 0) / 1e6
                equity = (_sf(latest.get("total_hldr_eqy_exc_min_int")) or 0) / 1e6

                ibd = 0.0
                for c in ["st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y"]:
                    v = _sf(latest.get(c))
                    if v:
                        ibd += v / 1e6

                result["cash"] = cash
                result["ibd"] = ibd
                result["goodwill"] = goodwill
                result["total_assets"] = ta

                # Goodwill / total assets
                if ta > 0:
                    result["goodwill_ratio"] = goodwill / ta * 100

                # Net debt / EBITDA (computed after EBITDA)

        # Process cashflow
        fcf_list = []
        if not cf_df.empty:
            cf_df = cf_df.sort_values("end_date", ascending=False)
            annual_cf = cf_df[cf_df["end_date"].str.endswith("1231")]

            if not annual_cf.empty:
                latest = annual_cf.iloc[0]
                ocf = (_sf(latest.get("n_cashflow_act")) or 0) / 1e6
                capex = (_sf(latest.get("c_pay_acq_const_fiolta")) or 0) / 1e6
                da = 0.0
                for c in ["depr_fa_coga_dpba", "amort_intang_assets", "lt_amort_deferred_exp"]:
                    v = _sf(latest.get(c))
                    if v:
                        da += v / 1e6

                result["da"] = da
                result["fcf"] = ocf - capex

                # FCF yield
                if mkt_cap > 0:
                    result["fcf_yield"] = (ocf - capex) / mkt_cap * 100

            # FCF consistency (positive years / total years, up to 5)
            for _, row in annual_cf.head(5).iterrows():
                o = _sf(row.get("n_cashflow_act"))
                c = _sf(row.get("c_pay_acq_const_fiolta"))
                if o is not None and c is not None:
                    fcf_list.append(o - c)

        if fcf_list:
            result["fcf_positive_years"] = sum(1 for f in fcf_list if f > 0)
            result["fcf_total_years"] = len(fcf_list)
            result["fcf_consistency"] = result["fcf_positive_years"] / result["fcf_total_years"]

        # FCF margin = FCF / Revenue * 100 (both in 百万元)
        rev = result.get("revenue")
        fcf_val = result.get("fcf")
        if rev is not None and rev > 0 and fcf_val is not None:
            result["fcf_margin"] = fcf_val / rev * 100

        # Compute composite metrics
        oper_profit = result.get("oper_profit")
        fin_exp = _sf(income_df.iloc[0].get("finance_exp")) / 1e6 if not income_df.empty and income_df.iloc[0].get("finance_exp") is not None else 0
        da = result.get("da", 0)
        cash = result.get("cash", 0)
        ibd = result.get("ibd", 0)
        np_parent = result.get("np_parent")

        # EBITDA: prefer fina_indicator, fallback to manual
        ebitda = None
        if fi_ebitda is not None:
            ebitda = fi_ebitda / 1e6
        elif oper_profit is not None:
            ebitda = oper_profit + (fin_exp or 0) + da

        # Net debt: prefer fina_indicator, fallback to manual
        if fi_netdebt is not None:
            net_debt_m = fi_netdebt / 1e6
        else:
            net_debt_m = ibd - cash

        if ebitda is not None:
            result["ebitda"] = ebitda
            ev = mkt_cap + net_debt_m
            result["ev"] = ev

            if ebitda > 0:
                result["ev_ebitda"] = ev / ebitda
                result["net_debt_ebitda"] = net_debt_m / ebitda

        # FCF: prefer fina_indicator, fallback to manual (already set above)
        if fi_fcff is not None:
            result["fcf"] = fi_fcff / 1e6
            if mkt_cap > 0:
                result["fcf_yield"] = (fi_fcff / 1e6) / mkt_cap * 100

        net_cash = -net_debt_m
        if np_parent is not None and np_parent > 0:
            result["cash_adj_pe"] = (mkt_cap - net_cash) / np_parent

        return result

    # ---- Tier 2: Floor price ----

    def _extract_floor_price(self, ts_code: str, close: float,
                             total_mv_wan: float) -> dict[str, Any]:
        """Extract 5-method floor price and composite baseline.

        Returns dict with baseline methods and premium analysis.
        """
        result: dict[str, Any] = {}
        total_shares = total_mv_wan * 10000 / close if close > 0 else 0  # 股

        try:
            bs_df = self._cached_call("balancesheet", ts_code=ts_code,
                                      report_type="1")
            cf_df = self._cached_call("cashflow", ts_code=ts_code)
            weekly_df = self._cached_call("weekly", ts_code=ts_code)
            div_df = self._cached_call("dividend", ts_code=ts_code)
        except Exception:
            return result

        def _sf(val):
            if val is None:
                return None
            try:
                f = float(val)
                return None if f != f else f
            except (TypeError, ValueError):
                return None

        baselines = []

        # ① Net liquid assets / share
        if not bs_df.empty:
            bs_df = bs_df.sort_values("end_date", ascending=False)
            annual_bs = bs_df[bs_df["end_date"].str.endswith("1231")]
            if not annual_bs.empty and total_shares > 0:
                latest = annual_bs.iloc[0]
                cash = _sf(latest.get("money_cap")) or 0
                trad = _sf(latest.get("trad_asset")) or 0
                ibd = 0.0
                for c in ["st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y"]:
                    v = _sf(latest.get(c))
                    if v:
                        ibd += v
                nla = (cash + trad - ibd) / total_shares
                baselines.append(("net_liquid_assets", nla))

                # ② BVPS
                equity = _sf(latest.get("total_hldr_eqy_exc_min_int")) or 0
                bvps = equity / total_shares
                baselines.append(("bvps", bvps))

        # ③ 历史最低价（列名兼容：zzshare 聚合给「收盘价」，
        #    旧 akshare 路径给 close）
        #    ⚠️ 窗口 = _zzshare_weekly 默认 4 年（非 10 年），标签须与之一致，
        #       否则读报告的人会以为这是十年低点。
        if not weekly_df.empty:
            _ccol = "close" if "close" in weekly_df.columns else "收盘价"
            if _ccol in weekly_df.columns:
                min_close = pd.to_numeric(weekly_df[_ccol], errors="coerce").dropna().min()
                if min_close is not None and min_close == min_close:
                    baselines.append(("4yr_low", float(min_close)))

        # ④ Dividend implied price
        rf = self._rf_cache
        if not div_df.empty and rf is not None:
            div_df = div_df.sort_values("end_date", ascending=False)
            recent_dps = []
            for _, row in div_df.head(3).iterrows():
                v = _sf(row.get("cash_div_tax"))
                if v is not None:
                    recent_dps.append(v)
            if recent_dps:
                avg_dps = sum(recent_dps) / len(recent_dps)
                discount = max(rf / 100, 0.03)
                implied = avg_dps / discount
                baselines.append(("dividend_implied", implied))

        # ⑤ Pessimistic FCF capitalization
        if not cf_df.empty and rf is not None and rf > 0 and total_shares > 0:
            cf_df = cf_df.sort_values("end_date", ascending=False)
            annual_cf = cf_df[cf_df["end_date"].str.endswith("1231")]
            fcf_list = []
            for _, row in annual_cf.head(5).iterrows():
                o = _sf(row.get("n_cashflow_act"))
                c = _sf(row.get("c_pay_acq_const_fiolta"))
                if o is not None and c is not None:
                    fcf_list.append(o - c)
            if fcf_list and min(fcf_list) > 0:
                min_fcf = min(fcf_list)
                cap_price = min_fcf / (rf / 100) / total_shares
                baselines.append(("pessimistic_fcf", cap_price))

        result["baselines"] = baselines

        # Composite = arithmetic mean of valid methods
        valid_prices = [v for _, v in baselines]
        if valid_prices:
            composite = sum(valid_prices) / len(valid_prices)
            result["composite_baseline"] = composite
            result["premium"] = (close / composite - 1) * 100 if composite > 0 else None
        else:
            result["composite_baseline"] = None
            result["premium"] = None

        return result

    # ---- Tier 2: Analyze single stock ----

    def _analyze_single_stock(self, row: pd.Series,
                              idx: int = 0) -> dict[str, Any] | None:
        """Run full Tier 2 analysis on a single stock.

        idx: 股票在 Tier2 队列中的序号 —— 用于六源轮换调度
             （同一只股票的三表会从不同源开始，防止单源压力集中）。

        Returns dict of all metrics, or None if vetoed.
        """
        # 记录否决原因：返回 None 时，调用方从 self._last_veto 读取。
        self._last_veto = ""
        # 设置轮换序号（适配器通过 contextvar 读取）
        try:
            from datasource.adapters.akshare_adapter import AkshareAdapter
            AkshareAdapter.set_rotation_index(idx)
        except Exception:                             # noqa: BLE001
            pass

        ts_code = row["ts_code"]
        channel = row.get("channel", "main")
        close = float(row.get("close", 0))
        total_mv_wan = float(row.get("total_mv", 0))

        result = {
            "ts_code": ts_code,
            "name": row.get("name", ""),
            "industry": row.get("industry", ""),
            "channel": channel,
            "close": close,
            "total_mv": total_mv_wan,
            "pe_ttm": row.get("pe_ttm"),
            "pb": row.get("pb"),
            "dv_ttm": row.get("dv_ttm"),
            # 质量指标：由 Tier1 批量源带入（Tier2 不再逐股重判）
            "roe_waa": row.get("roe_waa"),
            "gross_margin": row.get("grossprofit_margin"),
            "debt_to_assets": row.get("debt_to_assets"),
            "pledge_ratio": row.get("pledge_ratio"),
        }

        # Step 1: Hard vetoes (early termination)
        passed, reason = self._check_hard_vetoes(ts_code)
        if not passed:
            result["veto"] = reason
            self._clear_stock_cache(ts_code)
            self._last_veto = result.get("veto") or "unknown"
            return None

        # Step 2: 财务质量（ROE/毛利率/负债率）
        #   已【完全前移至 Tier1】且 Tier1 三项均已生效（含负债率 —— 由
        #   zzshare balance 自算，100% 覆盖含银行）→ Tier2 不再重判，
        #   节省 333 只 × 逐股请求。
        #   注：原 _check_financial_quality 保留供参考，但不再调用。

        # Step 2b: 观察通道 —— 放宽质量门槛（FCF 信号替代 ROE≥8%）
        #   亏损股（负 PE）无法用 ROE 门槛，改用原框架的 FCF 信号：
        #     ROE ≥ min_roe_obs(0) / 毛利率≥15% / 负债率≤70% /
        #     最近一年 OCF>0 / FCF 率≥0% / 5 年中 ≥2 年 FCF 为正。
        #   注：Tier1 已给观察通道灌好 roe/gross/debt 字段（见 6b2），
        #       ROE 门槛已禁用（min_roe_obs=-999），实际生效：
        #       毛利率≥15% / 负债率≤70% / OCF>0 / FCF率≥0% / FCF 正年≥2。
        if channel == "observation":
            passed, obs_m = self._check_obs_quality(ts_code, result, self.config)
            result.update(obs_m)
            if not passed:
                result["veto"] = "obs_quality"
                self._clear_stock_cache(ts_code)
                self._last_veto = result.get("veto") or "unknown"
                return None

        # Step 2c: 【ROE 质量】（2026-02 简化：不再逐股重算）
        #   历史演变：
        #     v1  zzshare 单季×4 累加（口径不准 + 部分相加 bug）
        #     v2  两级：Tier1 zzshare 累加(6%) / Tier2 新浪年报(8%)
        #     v3  当前：ROE 【统一用 Tier1 的 stock_yjbb_em 加权年报值】
        #   为何取消两级：Tier1 的源已换成 yjbb_em（东财加权口径，
        #     实测伊利 15.81 / 神华 13.55 / 招行 14.49，与人工核对值一致），
        #     数据准确度已足够，无需再用新浪逐股复核（省 5173 次请求）。
        #   Tier2 仅【复用】Tier1 传入的 roe_waa 与 min_roe 门槛做双保险。
        result["roe_refined"] = result.get("roe_waa")
        result["roe_refined_end"] = ""
        result["roe_refined_flag"] = ""
        if channel != "observation":
            _rv = result.get("roe_waa")
            if _rv is None:
                # Tier1 未传值（如单独调试单股）→ 标注待核，不剔除
                result["roe_refined_flag"] = "missing"
            elif _rv < self.config.min_roe:
                result["roe_refined_reject"] = True
                self._clear_stock_cache(ts_code)
                self._last_veto = result.get("veto") or "unknown"
                return None

        # Step 2d: 框架因子2 分配能力验证（一票否决）
        #   规则：FCF 长期为负 且 融资CF 持续为正 → 伪穿透回报 → 否决
        if self.config.tier2_apply_alloc_veto:
            alloc_ok, alloc_m = self._check_allocation_capacity(ts_code)
            result.update(alloc_m)
            if not alloc_ok:
                result["veto"] = ("allocation_capacity"
                                  f"(FCF负{alloc_m.get('alloc_fcf_neg_years')}/"
                                  f"{alloc_m.get('alloc_years')}年 + "
                                  f"融资正{alloc_m.get('alloc_fin_pos_years')}/"
                                  f"{alloc_m.get('alloc_years')}年)")
                self._clear_stock_cache(ts_code)
                self._last_veto = result.get("veto") or "unknown"
                return None

        # Step 2e: 【分红质量④条 —— 现状说明】（2026-02）
        #   用户原设计的四条质量条件，最终落地情况：
        #     ① Σ5年OCF ≥ Σ5年分红   → 【已删】zzshare OCF 数据错误
        #                                  （实测宇通 2023：-4.60 vs +47.17亿）
        #     ② 有息负债/净资产 ≤ 50%  → 【已删】借款科目全链路拿不到
        #                                  （balancesheet 返回无 st_borr 等）
        #     ③ 未分配利润 > 0         → 【已删】法定约束下几乎不触发
        #     ④ 分红后现金 ≥ 短债      → 【已改为】Tier1 速动比率 ≥ 0.8
        #   即：四条中仅第④条以「速动比率」形式在 Tier1 生效，
        #       其余三条因数据不可得或逻辑无效而不适用。
        #   替代保障：【Tier1 资产负债率 ≤ 70%】（银行 96%），
        #       数据完整（zzshare balance 100% 覆盖，含银行）。

        # Step 3: Factor 2 (penetration return)
        f2 = self._extract_factor2_metrics(ts_code, total_mv_wan)
        result["M"] = f2.get("M")
        # M_refined 为 Tier2 逐股口径（累计+中位数+分支锚定，R_refined 的推导据）；
        # Tier1 的智兔单年值保留在 payout_ratio 列（R_coarse 的推导据）。
        result["M_refined"] = f2.get("M_refined", f2.get("M"))
        result["M_refined_avg"] = f2.get("M_refined_avg")
        result["M_refined_median"] = f2.get("M_refined_median")
        result["M_refined_latest"] = f2.get("M_refined_latest")
        result["M_refined_flag"] = f2.get("M_refined_flag", "")
        result["M_refined_reject"] = f2.get("M_refined_reject")
        result["M_refined_diag"] = f2.get("M_refined_diag")

        # Step 3a: 【Tier2 M 否决】（2026-02）
        #   现在 M 完全【复用 Tier1 的 stock_fhps_em 结果】，M_reject 也是
        #   Tier1 算好的（前置过滤：累计净利≤0 / 正利润年<3）。
        #   注：Tier1 已在同一处用 M_reject 做过过滤，正常流程下这里不会
        #       触发；保留此判为【双保险】，防止 Tier1→Tier2 的传递遗漏。
        _mrej = result.get("M_refined_reject")
        if _mrej:
            result["veto"] = _mrej
            self._clear_stock_cache(ts_code)
            self._last_veto = result.get("veto") or "unknown"
            return None

        result["R"] = f2.get("R")
        result["II"] = f2.get("II")
        result["Rf"] = f2.get("Rf")
        result["R_vs_II"] = f2.get("R_vs_II")
        result["AA"] = f2.get("AA")
        result["AA_years"] = f2.get("AA_years")

        # Step 3b: 粗算 R（框架原生，Tier1 已传）
        #   R_coarse = C × M / 市值（粗算，假设净利全可分）
        #                C/M 均用 Tier1 智兔单年口径
        #   R        = AA × M / 市值（精算，AA=真实可支配现金）
        #                此处 M 用 Tier2 的 M_refined（5 年均值锚定）
        #   两者差异 > 30% → 现金质量与净利背离，需人工看。
        result["R_refined"] = f2.get("R")          # 精算（AA × M_refined 口径）
        result["R_coarse"] = row.get("R_coarse")   # 粗算（净利 × Tier1 M 口径）
        # ⚠️ 两个 payout 分开存，避免同名不同义（2026-02 修复）：
        #   payout_ratio      = Tier1 智兔单年值（R_coarse 的推导据）
        #   M_refined         = Tier2 逐股 5 年锚定值（R_refined 的推导据）
        result["payout_ratio"] = row.get("payout_ratio")
        result["net_profit"] = row.get("net_profit")
        _rc, _rr = result.get("R_coarse"), result.get("R_refined")
        if _rc is not None and _rr is not None and _rr != 0:
            result["R_divergence"] = abs(_rc - _rr) / abs(_rr) * 100
        else:
            result["R_divergence"] = None

        # Step 3c: 框架「粗算否决门」—— R < II×0.5 直接剔除
        #   注：Tier1 已筛过一轮，此处用【精算 R（AA 口径）】再确认。
        #   若精算 R 低于门槛而粗算达标 → 说明 AA 远低于净利（现金质量差），
        #   正是框架「因子2粗算→因子3精算」要敲出来的情形。
        _ii = result.get("II")
        _cfg = self.config
        if (_cfg.tier2_apply_R_veto and _ii is not None
                and result.get("R_refined") is not None
                and result["R_refined"] < _ii * _cfg.tier1_R_floor_mult):
            result["veto"] = "R_below_floor(AA口径)"
            self._clear_stock_cache(ts_code)
            self._last_veto = result.get("veto") or "unknown"
            return None

        # Step 4: Factor 4 (valuation)
        f4 = self._extract_factor4_metrics(ts_code, close, total_mv_wan)
        result["ev_ebitda"] = f4.get("ev_ebitda")
        result["cash_adj_pe"] = f4.get("cash_adj_pe")
        result["fcf_yield"] = f4.get("fcf_yield")
        result["net_debt_ebitda"] = f4.get("net_debt_ebitda")
        result["goodwill_ratio"] = f4.get("goodwill_ratio")
        result["fcf_consistency"] = f4.get("fcf_consistency")
        result["fcf_margin"] = f4.get("fcf_margin")

        # Step 5: Floor price
        fp = self._extract_floor_price(ts_code, close, total_mv_wan)
        result["floor_baseline"] = fp.get("composite_baseline")
        result["floor_premium"] = fp.get("premium")

        self._clear_stock_cache(ts_code)
        return result

    # ---- Scoring ----

    def _compute_rankings(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute composite score based on percentile rankings across 5 dimensions."""
        if df.empty:
            return df

        cfg = self.config
        df = df.copy()

        # Percentile rank for each dimension
        # Higher is better: ROE, FCF yield, penetration R
        # 排序用 R_used：优先粗算 R_coarse（全市场可比），缺失时回退精算 R_refined。
        # ⚠️ 不覆盖 R 列（R = 精算 AA 口径，与 R_coarse 并列保留给报告看）。
        _coarse = df["R_coarse"] if "R_coarse" in df.columns else pd.Series(index=df.index, dtype=float)
        _refined = df["R_refined"] if "R_refined" in df.columns else df.get("R")
        df["R_used"] = _coarse.where(_coarse.notna(), _refined)
        for col in ["roe_waa", "fcf_yield", "R_used"]:
            if col in df.columns:
                df[f"{col}_pctile"] = df[col].rank(pct=True, na_option="bottom")
            else:
                df[f"{col}_pctile"] = 0.0

        # Lower is better: EV/EBITDA, floor premium
        for col in ["ev_ebitda", "floor_premium"]:
            if col in df.columns:
                df[f"{col}_pctile"] = 1.0 - df[col].rank(pct=True, na_option="top")
            else:
                df[f"{col}_pctile"] = 0.0

        # Composite score
        df["composite_score"] = (
            cfg.weight_roe * df["roe_waa_pctile"] +
            cfg.weight_fcf_yield * df["fcf_yield_pctile"] +
            cfg.weight_penetration_r * df["R_used_pctile"] +
            cfg.weight_ev_ebitda * df["ev_ebitda_pctile"] +
            cfg.weight_floor_premium * df["floor_premium_pctile"]
        )

        # ── 框架因子4【安全边际 KK】 ──
        #   KK = GG − II。GG 取 R_used（优先粗算 R_coarse，回退精算 R_refined）。
        #
        #   ⚠️ 这里【只给 KK 数值，不给仓位结论】。
        #   框架的仓位矩阵是三维：安全边际 KK × 外推可信度 × 陷阱风险，
        #   而后两维依赖定性（护城河/行业/管理层/经营模式变化）。
        #   选股器只有第一维 → 给不出可靠仓位。
        #   仓位矩阵由下游 valuation agent 产出（见 agents/valuation.md 步骤3）。
        df["KK"] = df.apply(
            lambda r: (r.get("R_used") - r.get("II"))
            if (pd.notna(r.get("R_used")) and pd.notna(r.get("II"))) else None,
            axis=1)

        df = df.sort_values("composite_score", ascending=False)
        return df

    # ---- Pipeline ----

    def run(self, tier1_only: bool = False, tier2_limit: int | None = None,
            progress_callback=None) -> pd.DataFrame:
        """Run full screening pipeline.

        Args:
            tier1_only: If True, stop after Tier 1 (no deep analysis).
            tier2_limit: Override tier2_max_stocks for testing.
            progress_callback: Optional callable(current, total, ts_code) for progress.

        Returns:
            DataFrame with screening results.
        """
        # Tier 1
        print("=== Tier 1: Bulk screening ===")
        bulk_df = self._tier1_bulk_data()
        print(f"  Universe: {len(bulk_df)} stocks")

        filtered = self._tier1_filter(bulk_df)
        print(f"  After filters: {len(filtered)} stocks "
              f"(main: {len(filtered[filtered['channel']=='main'])}, "
              f"observation: {len(filtered[filtered['channel']=='observation'])})")

        ranked = self._tier1_rank_and_cut(filtered)
        print(f"  After rank & cut: {len(ranked)} stocks")

        if tier1_only:
            return ranked

        # Tier 2
        if tier2_limit is not None:
            ranked = ranked.head(tier2_limit)

        total = len(ranked)
        print(f"\n=== Tier 2: Deep analysis ({total} stocks) ===")
        cfg = self.config

        # ── 断点续跑：从已有进度文件恢复 ──
        #   为何需要：333 只约 20 分钟，中途可能遭上游限流/网络中断。
        #   每算完一只立即落盘 Tushare 风格 JSON，重跑时自动跳过。
        ckpt_path = os.environ.get("SCREENER_TIER2_CKPT") or os.path.join(
            cfg.cache_dir, "tier2_progress.json")
        results: list[dict] = []
        done: set[str] = set()
        if os.environ.get("SCREENER_TIER2_RESUME", "1") != "0" \
                and os.path.isfile(ckpt_path):
            try:
                with open(ckpt_path, encoding="utf-8") as f:
                    saved = json.load(f)
                results = saved.get("results") or []
                done = {r.get("ts_code") for r in results if r.get("ts_code")}
                print(f"  [续跑] 已有进度 {len(done)} 只，跳过")
            except Exception as e:                   # noqa: BLE001
                print(f"  [续跑] 进度文件读取失败（忽略）：{str(e)[:60]}")
                results, done = [], set()

        def _save() -> None:
            try:
                os.makedirs(os.path.dirname(ckpt_path), exist_ok=True)
                with open(ckpt_path, "w", encoding="utf-8") as f:
                    json.dump({"results": results, "total": total}, f,
                              ensure_ascii=False)
            except Exception:                        # noqa: BLE001
                pass

        vetoed = {"pledge": 0, "audit": 0, "financial_quality": 0}
        # 限流信号：连续多次「全接口失败」则怀疑被限流，暂停
        _consec_fail = 0
        _MAX_CONSEC_FAIL = int(os.environ.get("SCREENER_MAX_CONSEC_FAIL", "5"))

        for i, (_, row) in enumerate(ranked.iterrows()):
            ts_code = row["ts_code"]
            if ts_code in done:
                continue
            if progress_callback:
                progress_callback(i + 1, total, ts_code)
            else:
                print(f"  [{i+1}/{total}] {ts_code} {row.get('name', '')}...", end="")

            try:
                stock_result = self._analyze_single_stock(row, idx=i)
            except Exception as e:                   # noqa: BLE001
                # 网络/限流导致单只失败：不计入否决，记为空结果并继续
                stock_result = None
                _consec_fail += 1
                print(f" ERR({type(e).__name__})")
                if _consec_fail >= _MAX_CONSEC_FAIL:
                    _save()
                    print(f"\n  ⚠️ 连续 {_consec_fail} 只失败，疑似上游限流/断网。"
                          f"\n  已完成 {len(results)} 只，进度已保存到：{ckpt_path}"
                          f"\n  稍后重跑即从断点继续（设 SCREENER_TIER2_RESUME=0 可强制重来）。")
                    break
                continue

            if stock_result is None:
                # 原因从 _analyze_single 的日志/返回中取不到（它返回 None），
                # 改为取最近一次记录的 _last_veto 原因（见 _analyze_single）。
                _vreason = getattr(self, "_last_veto", "unknown")
                print(f" VETOED({_vreason})")
                # 被否决的也要记进度，否则重跑时会重算；
                # 保留真实否决原因，便于审计（旧实现只存 '_vetoed_'）。
                results.append({"ts_code": ts_code, "name": row.get("name", ""),
                                "veto": _vreason or "unknown"})
                _save()
            else:
                _consec_fail = 0
                results.append(stock_result)
                print(" OK")
                _save()                              # 增量落盘

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)
        # 拆分：被否决（记录真实原因，供审计/续跑）+ 通过
        # 旧实现直接丢掉被否决行，导致「谁被什么条件筛掉」无法追溯。
        if "veto" in result_df.columns:
            self._tier2_vetoed = result_df[result_df["veto"].notna()].copy()
            result_df = result_df[result_df["veto"].isna()].copy()
        result_df = result_df.drop(columns=["veto"], errors="ignore")
        if result_df.empty:
            return pd.DataFrame()

        # Compute rankings
        result_df = self._compute_rankings(result_df)

        print(f"\n=== Results: {len(result_df)} stocks passed ===")
        return result_df

    # ---- Export ----

    def export_csv(self, df: pd.DataFrame, path: str) -> None:
        """Export results to CSV."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"Exported to {path}")

    def export_html(self, df: pd.DataFrame, path: str) -> None:
        """Export styled HTML report."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        # Select display columns
        display_cols = [c for c in [
            "ts_code", "name", "industry", "close", "pe_ttm", "pb", "dv_ttm",
            "roe_waa", "roe_refined", "gross_margin", "fcf_yield", "fcf_margin",
            "R_coarse", "R_refined", "R_divergence", "payout_ratio",
            "M_refined", "M_refined_median", "M_refined_flag",
            "alloc_years", "alloc_fcf_neg_years", "alloc_fin_pos_years",
            "ev_ebitda", "floor_premium", "composite_score"
        ] if c in df.columns]

        display_df = df[display_cols].copy()

        html = display_df.to_html(index=False, float_format="%.2f",
                                  classes="screener-table")

        full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>龟龟选股器 Results</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 20px; }}
.screener-table {{ border-collapse: collapse; width: 100%; }}
.screener-table th {{ background: #2c3e50; color: white; padding: 8px 12px; text-align: left; }}
.screener-table td {{ border-bottom: 1px solid #ddd; padding: 6px 12px; }}
.screener-table tr:hover {{ background: #f5f5f5; }}
</style>
</head>
<body>
<h1>龟龟选股器 — 筛选结果</h1>
<p>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 共 {len(df)} 只股票</p>
{html}
</body>
</html>"""

        with open(path, "w", encoding="utf-8") as f:
            f.write(full_html)
        print(f"Exported to {path}")


# ============================================================
# CLI
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="龟龟选股器 (Turtle Screener)")
    parser.add_argument("--tier1-only", action="store_true",
                        help="Only run Tier 1 (bulk screening, ~5s)")
    parser.add_argument("--tier2-limit", type=int, default=None,
                        help="Limit number of stocks for Tier 2 (default: 200)")
    parser.add_argument("--min-roe", type=float, default=None,
                        help="Override Tier1 COARSE ROE threshold (%%) [default 6]")
    parser.add_argument("--min-roe-refined", type=float, default=None,
                        help="Override Tier2 REFINED ROE threshold (%%) [default 8]")
    parser.add_argument("--max-pe", type=float, default=None,
                        help="Override maximum PE threshold")
    parser.add_argument("--min-gross-margin", type=float, default=None,
                        help="Override minimum gross margin (%%)")
    parser.add_argument("--csv", type=str, default=None,
                        help="Export results to CSV file")
    parser.add_argument("--html", type=str, default=None,
                        help="Export results to HTML file")
    parser.add_argument("--output", type=str, default=None,
                        help="Output directory (default: output/)")
    parser.add_argument("--cache-refresh", action="store_true",
                        help="Force refresh all cached data")
    parser.add_argument("--cache-tier2-refresh", action="store_true",
                        help="Clear Tier 2 per-stock disk cache (keep Tier 1)")

    args = parser.parse_args()

    # Build config with overrides
    overrides = {}
    if args.min_roe is not None:
        overrides["min_roe"] = args.min_roe
    if args.min_roe_refined is not None:
        overrides["min_roe_refined"] = args.min_roe_refined
    if args.max_pe is not None:
        overrides["max_pe"] = args.max_pe
    if args.min_gross_margin is not None:
        overrides["min_gross_margin"] = args.min_gross_margin

    config = ScreenerConfig.from_dict(overrides) if overrides else ScreenerConfig()

    screener = TushareScreener(config=config)

    if args.cache_refresh:
        screener.cache.clear()
    elif args.cache_tier2_refresh:
        screener.cache.invalidate_prefix("tier2_")
        screener.cache.invalidate_prefix("global_")

    # Run
    try:
        from tqdm import tqdm
        pbar = tqdm(total=0, desc="Tier 2")

        def _progress(current, total, ts_code):
            pbar.total = total
            pbar.n = current
            pbar.set_postfix(stock=ts_code)
            pbar.refresh()

        result = screener.run(
            tier1_only=args.tier1_only,
            tier2_limit=args.tier2_limit,
            progress_callback=None if args.tier1_only else _progress,
        )
        pbar.close()
    except ImportError:
        result = screener.run(
            tier1_only=args.tier1_only,
            tier2_limit=args.tier2_limit,
        )

    if result.empty:
        print("No results.")
        return

    # Display top results
    display_cols = [c for c in [
        "ts_code", "name", "industry", "composite_score",
        "roe_waa", "roe_refined", "roe_refined_end", "roe_refined_flag",
        "fcf_yield", "fcf_margin", "R_coarse", "R_refined", "R_divergence",
        "payout_ratio", "M_refined", "M_refined_avg", "M_refined_latest",
        "ev_ebitda", "floor_premium"
    ] if c in result.columns]
    print("\n" + result[display_cols].head(20).to_string(index=False))

    # Export
    output_dir = args.output or "output"
    if args.csv:
        screener.export_csv(result, args.csv)
    if args.html:
        screener.export_html(result, args.html)

    # Default export
    if not args.csv and not args.html and not args.tier1_only:
        csv_path = os.path.join(output_dir, "screener_results.csv")
        screener.export_csv(result, csv_path)


if __name__ == "__main__":
    main()
