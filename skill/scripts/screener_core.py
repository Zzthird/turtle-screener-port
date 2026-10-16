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
    "pledge_stat": "ts_code,end_date,pledge_count,pledge_ratio",
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
    "pledge_stat": "financial",
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
        #    Tushare returns NaN pe_ttm for loss-making stocks (not negative)
        pe_valid = df["pe_ttm"].notna()
        main_mask = pe_valid & (df["pe_ttm"] > 0) & (df["pe_ttm"] <= cfg.max_pe)
        obs_mask = ~pe_valid  # NaN PE = loss-making → observation channel

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
                # 注：银行在 /hicw/cznl 中整条缺记录（实测 601398/601166 均无），
                # 此处不再逐股补值 —— 银行豁免阈值为 96%，实测负债率 91~93%
                # 本就通过，补值拦不住任何一只（纯浪费 40 次请求）。
                # 若报告需展示银行负债率，由 Tier2 的 /hs/fin/ratios 补齐。

                before = len(main_df)
                # ROE：均有门槛（年度值，由 4 季累加得到）
                ok = main_df["roe_waa"].isna() | (main_df["roe_waa"] >= cfg.min_roe)
                # 毛利率：银行为 0（不适用），其余 15%
                gm_min = main_df["_bank"].map(
                    lambda b: cfg.min_gross_margin_bank if b else cfg.min_gross_margin)
                ok &= main_df["grossprofit_margin"].isna() | \
                      (main_df["grossprofit_margin"] >= gm_min)
                # 负债率：批量源拿不到（zzshare 无此列），交由 Tier2 判；
                # 若已拿到则同样过滤
                debt_max = main_df["_bank"].map(
                    lambda b: cfg.max_debt_ratio_bank if b else cfg.max_debt_ratio)
                ok &= main_df["debt_to_assets"].isna() | \
                      (main_df["debt_to_assets"] <= debt_max)
                main_df = main_df[ok].drop(columns=["_ind", "_bank"])
                if os.environ.get("SCREENER_VERBOSE") == "1":
                    print(f"  [Tier1 质量门槛前移] {before} -> {len(main_df)} 只"
                          f"（ROE>={cfg.min_roe}% / 毛利率>={cfg.min_gross_margin}%"
                          f" / 负债率<={cfg.max_debt_ratio}%）")

        # 6c. 【框架因子2：粗算穿透回报率 R】
        #     R = 归母净利 C × 支付率 M / 市值   （框架「粗算 R」定义）
        #     否决门：R < II × 0.5 （框架因子2 粗算否决门）
        #     ★ 上游将 R 放 Tier2 是因 Tushare 需逐股拉；
        #       本项 C/M/市值 均由智兔批量一次拿到（~1 次请求/5000 只），
        #       故可下放到 Tier1。
        #     同时保留支付率上限（M <= tier1_max_payout，剔除借债分红）。
        if cfg.tier1_apply_quality and not main_df.empty:
            try:
                po = self._backend.payout_all()
            except Exception:                          # noqa: BLE001
                po = None
            if po is not None and len(po):
                po_idx = po.set_index("ts_code")
                main_df["payout_ratio"] = main_df["ts_code"].map(po_idx["payout_ratio"])
                main_df["net_profit"] = main_df["ts_code"].map(po_idx["net_profit"])

                # 粗算 R(%) = C(百万元) × M(%) / 市值(百万元)
                mcap = main_df["total_mv"] * 10000 / 1e6      # 万元 -> 百万元
                main_df["R_coarse"] = (
                    main_df["net_profit"] * (main_df["payout_ratio"] / 100)
                    / mcap * 100).where(mcap > 0)

                before = len(main_df)
                # (a) 支付率上限
                ok = main_df["payout_ratio"].isna() | \
                     (main_df["payout_ratio"] <= cfg.tier1_max_payout)
                # (b) 粗算 R 否决门：R >= II×0.5（缺失值放行）
                _ii = getattr(self, "_tier1_ii", None)
                if _ii is not None and cfg.tier1_apply_R:
                    ok &= main_df["R_coarse"].isna() | \
                          (main_df["R_coarse"] >= _ii * cfg.tier1_R_floor_mult)
                main_df = main_df[ok]
                if os.environ.get("SCREENER_VERBOSE") == "1":
                    _msg = (f"  [Tier1 支付率上限] {before} -> {len(main_df)} 只"
                            f"（M<={cfg.tier1_max_payout}%")
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
        """Check pledge ratio and audit opinion.

        Returns (passed, reason). If passed=False, reason explains why.
        """
        cfg = self.config

        # Check pledge ratio
        try:
            pledge_df = self._cached_call("pledge_stat", ts_code=ts_code)
            if not pledge_df.empty:
                pledge_df = pledge_df.sort_values("end_date", ascending=False)
                ratio = pledge_df.iloc[0].get("pledge_ratio")
                if ratio is not None and not (ratio != ratio):  # NaN check
                    if float(ratio) > cfg.max_pledge_pct:
                        return False, f"pledge_ratio={ratio:.1f}% > {cfg.max_pledge_pct}%"
        except Exception:
            pass  # non-fatal: data may not be available

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

        return True, metrics

    def _check_obs_quality(self, ts_code: str, metrics: dict[str, Any],
                           cfg: "ScreenerConfig") -> tuple[bool, dict[str, Any]]:
        """Observation-channel quality check: FCF-based signals.

        Gates:
        1. ROE >= min_roe_obs (default 0%, relaxed from 8%)
        2. Gross margin >= min_gross_margin (same as main, 15%)
        3. Debt ratio <= max_debt_ratio (same as main, 70%)
        4. OCF > 0 (if obs_require_ocf_positive)
        5. FCF margin (FCF/Revenue) >= min_fcf_margin_obs (default 0%)
        6. FCF positive years >= min_fcf_positive_years_obs (default 2 of 5)
        """
        # Gate 1: Relaxed ROE
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

        # Compute 3yr avg payout ratio M
        payout_ratios = []
        if not div_df.empty and not annual_inc.empty:
            div_df = div_df.sort_values("end_date", ascending=False)
            div_lookup = {}
            for _, r in div_df.iterrows():
                y = str(r.get("end_date", ""))[:4]
                cash_div = r.get("cash_div_tax")
                base_share = r.get("base_share")
                if (cash_div is not None and pd.notna(cash_div)
                        and base_share is not None and pd.notna(base_share)):
                    try:
                        div_total = float(cash_div) * float(base_share) * 10000 / 1e6  # 百万元
                        div_lookup[y] = div_total
                    except (ValueError, TypeError):
                        pass

            years = [str(r["end_date"])[:4] for _, r in annual_inc.head(3).iterrows()]
            for y in years:
                div_total = div_lookup.get(y)
                for _, r in annual_inc.iterrows():
                    if str(r["end_date"])[:4] == y:
                        np_val = r.get("n_income_attr_p")
                        if np_val is not None:
                            try:
                                np_f = float(np_val) / 1e6  # 百万元
                                if div_total and np_f > 0:
                                    payout_ratios.append(div_total / np_f * 100)
                            except (ValueError, TypeError):
                                pass
                        break

        if payout_ratios:
            M = sum(payout_ratios) / len(payout_ratios)
            result["M"] = M
        else:
            result["M"] = None

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

            aa_vals = []
            for _, cf_row in annual_cf.head(aa_win).iterrows():
                yr = str(cf_row["end_date"])[:4]
                inc_row = _year_row(annual_inc, yr)
                bs_row = _year_row(bs_df, yr)
                bs_prev = _year_row(bs_df, str(int(yr) - 1))
                if bs_prev is None:
                    bs_prev = bs_row
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
                ar_now = _pick(bs_row, "accounts_receiv", "应收账款")
                ar_prev = _pick(bs_prev, "accounts_receiv", "应收账款")
                if ar_now is not None and ar_prev is not None:
                    T = ar_now - ar_prev
                cl_now = _pick(bs_row, "contract_liab", "合同负债")
                cl_prev = _pick(bs_prev, "contract_liab", "合同负债")
                if cl_now is not None and cl_prev is not None:
                    U = min(cl_now - cl_prev, 0.0)       # 仅净减扣，净增不加回
                cash_rev = S - T - abs(U)

                # ---- 经营性现金支出 W（§17.4）----
                W1 = _pick(inc_row, "oper_cost", "营业成本") or 0.0
                ap_now = _pick(bs_row, "acct_payable", "应付账款")
                ap_prev = _pick(bs_prev, "acct_payable", "应付账款")
                if ap_now is not None and ap_prev is not None:
                    W1 += (ap_prev - ap_now)              # 应付净减 -> 多付现金
                W2 = _pick(cf_row, "c_paid_to_for_empl", "支付给职工现金") or 0.0
                W3 = _pick(inc_row, "income_tax", "所得税") or 0.0
                W4 = _pick(inc_row, "fin_exp", "财务费用") or 0.0
                W = W1 + W2 + W3 + W4

                # ---- 总扣除 Y ----
                capex = abs(_pick(cf_row, "c_pay_acq_const_fiolta", "资本开支") or 0.0)
                Y = capex

                # ---- 非经常性流入 V1 / V5 ----
                v1 = _pick(cf_row, "c_disp_fiolta_recp", "处置固定资产收回") or 0.0
                v5 = _pick(cf_row, "c_recp_return_invest", "取得投资收益收到") or 0.0

                # ---- 字段完整度保护 ----
                # 免费源的利润表缺 income_tax / fin_exp（值全 None）。
                # 若 W3=W4=0，则 W 只剩「成本+应付」，会系统性【低估支出】
                # 而【高估 AA】。此时改用 OCF 作为支出总额的上限约束：
                #   AA ≈ min(真实现金收入, OCF) + V1 + V5 - Capex
                # 其理由：OCF = 真实现金收入 - W（近似），故被 min 截住不会虚高。
                if W3 == 0.0 and W4 == 0.0:
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

        # ③ 10-year low
        if not weekly_df.empty:
            min_close = weekly_df["close"].dropna().min()
            if min_close is not None and min_close == min_close:
                baselines.append(("10yr_low", float(min_close)))

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

    def _analyze_single_stock(self, row: pd.Series) -> dict[str, Any] | None:
        """Run full Tier 2 analysis on a single stock.

        Returns dict of all metrics, or None if vetoed.
        """
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
        }

        # Step 1: Hard vetoes (early termination)
        passed, reason = self._check_hard_vetoes(ts_code)
        if not passed:
            result["veto"] = reason
            self._clear_stock_cache(ts_code)
            return None

        # Step 2: Financial quality
        fq_passed, fq_metrics = self._check_financial_quality(ts_code, channel)
        result.update(fq_metrics)
        if not fq_passed:
            result["veto"] = "financial_quality"
            self._clear_stock_cache(ts_code)
            return None

        # Step 3: Factor 2 (penetration return)
        f2 = self._extract_factor2_metrics(ts_code, total_mv_wan)
        result["M"] = f2.get("M")
        result["R"] = f2.get("R")
        result["II"] = f2.get("II")
        result["Rf"] = f2.get("Rf")
        result["R_vs_II"] = f2.get("R_vs_II")
        result["AA"] = f2.get("AA")
        result["AA_years"] = f2.get("AA_years")

        # Step 3b: 粗算 R（框架原生，Tier1 已传）
        #   R_coarse = C × M / 市值（粗算，假设净利全可分）
        #   R        = AA × M / 市值（精算，AA=真实可支配现金）
        #   两者差异 > 30% → 现金质量与净利背离，需人工看。
        result["R_refined"] = f2.get("R")          # 精算（AA 口径）
        result["R_coarse"] = row.get("R_coarse")   # 粗算（净利口径）
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

        results = []
        vetoed = {"pledge": 0, "audit": 0, "financial_quality": 0}

        for i, (_, row) in enumerate(ranked.iterrows()):
            ts_code = row["ts_code"]
            if progress_callback:
                progress_callback(i + 1, total, ts_code)
            else:
                print(f"  [{i+1}/{total}] {ts_code} {row.get('name', '')}...", end="")

            stock_result = self._analyze_single_stock(row)

            if stock_result is None:
                print(" VETOED")
            else:
                results.append(stock_result)
                print(" OK")

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)

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
            "roe_waa", "gross_margin", "fcf_yield", "fcf_margin",
            "R_coarse", "R_refined", "R_divergence", "payout_ratio",
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
                        help="Override minimum ROE threshold (%%)")
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
        "ts_code", "name", "industry", "composite_score", "roe_waa",
        "fcf_yield", "fcf_margin", "R_coarse", "R_refined", "R_divergence",
        "payout_ratio", "ev_ebitda", "floor_premium"
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
