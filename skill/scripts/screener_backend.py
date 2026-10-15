"""选股器的免费数据源后端。

为 screener_core 提供 Tushare 兼容接口的数据，底层走 AkShare/东财。
无需 TUSHARE_TOKEN。

实测（2026-10）：
  - 全市场快照：AkShare stock_zh_a_spot_em（东财，push2 可能被风控）
  - 降级：AkShare stock_zh_a_spot（新浪源）
  - 财务指标：逐股取（慢，靠缓存）
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd


# ---------------------------------------------------------------------------
# 项目 .env 加载（避免污染系统环境变量）
# ---------------------------------------------------------------------------

def _load_project_env() -> None:
    """从项目根/.env 读取配置到进程环境（不覆盖已有值）。

    搜索顺序：cwd/.env → cwd/../.env → cwd/../../.env
    格式：KEY=VALUE（支持 # 注释、引号）
    """
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent,
                 Path(__file__).resolve().parent.parent.parent):
        env = base / ".env"
        if not env.exists():
            continue
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip("'\"")
                if k and v and k not in os.environ:
                    os.environ[k] = v
            return
        except Exception:                             # noqa: BLE001
            continue


_load_project_env()


class FreeScreenerBackend:
    """Tushare 兼容的免费数据后端。

    实现 screener_core 需要的接口：
      stock_basic     全市场股票列表
      daily_basic     全市场当日指标（PE/PB/市值）
      fina_indicator  单股财务指标
      trade_cal       交易日历
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._ak = None
        self._snapshot: Optional[pd.DataFrame] = None

    def _akshare(self):
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak

    # -- 全市场快照（stock_basic + daily_basic 合并）--------------------
    def snapshot(self) -> pd.DataFrame:
        """返回全市场快照，列名对齐 Tushare：
        ts_code, name, industry, area, market, list_date,
        trade_date, close, pe_ttm, pb, total_mv, circ_mv, dv_ttm, turnover_rate

        实测（2026-10）：`push2.eastmoney.com` 根域名被 IP 风控，
        但【数字前缀子域】（如 82.push2 / 2.push2）可用。
        """
        if self._snapshot is not None:
            return self._snapshot

        # 磁盘缓存（避免同日重复拉取全市场）
        cache = self._get_cache()
        if cache is not None:
            from rate_limit import SNAPSHOT_TTL
            cached = cache.get("snapshot_all", int(SNAPSHOT_TTL))
            if cached is not None and len(cached):
                if self.verbose:
                    print(f"[free-backend] 快照来源: 磁盘缓存 ({len(cached)} 行)")
                self._snapshot = cached
                return cached

        df = self._fetch_zzshare()          # 主源：一次拿全市场估值（稳定，无风控）
        if df is None or not len(df):
            df = self._fetch_em_push()      # 备源：东财 push2delay（含股息率）
        if df is None or not len(df):
            df = self._fetch_akshare_spot() # 降级：新浪（仅有价格）
        if df is None or not len(df):
            raise RuntimeError("全市场快照获取失败（zzshare/东财/新浪均不可用）")

        m = pd.DataFrame()
        m["ts_code"] = df.get("ts_code", pd.Series(dtype=str)).astype(str)
        m["name"] = df.get("name", pd.Series(dtype=str)).astype(str)
        m["close"] = pd.to_numeric(df.get("close"), errors="coerce")
        m["pe_ttm"] = pd.to_numeric(df.get("pe_ttm"), errors="coerce")
        m["pb"] = pd.to_numeric(df.get("pb"), errors="coerce")
        m["turnover_rate"] = pd.to_numeric(df.get("turnover_rate"), errors="coerce")
        # 市值源单位差异：东财(元) / zzshare(已转元) —— 统一为【万元】
        m["total_mv"] = pd.to_numeric(df.get("total_mv"), errors="coerce") / 1e4
        m["circ_mv"] = pd.to_numeric(df.get("circ_mv"), errors="coerce") / 1e4
        m["dv_ttm"] = pd.to_numeric(df.get("dv_ttm"), errors="coerce")
        today = _dt.datetime.now().strftime("%Y%m%d")
        m["trade_date"] = df.get("trade_date", today)
        m["industry"] = ""
        m["area"] = ""
        m["market"] = ""
        m["list_date"] = ""
        # 股息率：优先用源数据（push2 的 f37），降级为 None
        m["dv_ttm"] = pd.to_numeric(df.get("dv_ttm"), errors="coerce")
        self._snapshot = m
        # 写磁盘缓存
        if cache is not None:
            try:
                cache.put("snapshot_all", m)
            except Exception:                         # noqa: BLE001
                pass
        return m

    def _get_cache(self):
        """惰性创建磁盘缓存实例。"""
        if getattr(self, "_cache", None) is None:
            try:
                import os as _os
                from cache_utils import ScreenerCache
                d = _os.environ.get("SCREENER_CACHE_DIR") or _os.path.join("output", ".screener_cache")
                self._cache = ScreenerCache(d)
            except Exception:                         # noqa: BLE001
                self._cache = False
        return self._cache or None

    @staticmethod
    def _to_ts(code: str) -> str:
        """证券代码 -> Tushare 格式（带交易所后缀）。

        东财代码规律：
          6xxxxx/9xxxxx -> 沪   （北交所 920xxx 例外）
          00/30/2xxxxx  -> 深
          4xxxxx/8xxxxx -> 北交所
          920xxx        -> 北交所（新代码段）
        """
        c = str(code).strip()
        if "." in c:
            return c.upper()
        if c.startswith("920") or c.startswith(("4", "8")):
            return c + ".BJ"
        if c.startswith(("6", "9")):
            return c + ".SH"
        if c.startswith(("0", "2", "3")):
            return c + ".SZ"
        return c

    def _fetch_zzshare(self) -> Optional[pd.DataFrame]:
        """zzshare finance_valuation —— 一次返回全市场估值（5226 行）。

        字段: code, pe_ratio, pb_ratio, market_cap(亿元), capitalization(万股),
              circulation... , turnover_ratio, ps_ratio, pcf_ratio
        实测（2026-10）：无需 Cookie/代理，稳定可用。
        缺股息率（由东财源或本地计算补充）。
        """
        try:
            from zzshare.client import DataApi
        except ImportError:
            return None
        try:
            api = DataApi()
            # 找最近有数据的交易日（往前找 5 天）
            df = None
            today = _dt.date.today()
            for i in range(5):
                d = (today - _dt.timedelta(days=i)).strftime("%Y-%m-%d")
                try:
                    t = api.finance_valuation(d)
                except Exception:                     # noqa: BLE001
                    continue
                if t is not None and len(t):
                    df = t
                    break
            if df is None or not len(df):
                return None

            out = pd.DataFrame()
            out["ts_code"] = df["code"].astype(str)
            out["pe_ttm"] = pd.to_numeric(df.get("pe_ratio"), errors="coerce")
            out["pb"] = pd.to_numeric(df.get("pb_ratio"), errors="coerce")
            # 市值：zzshare 为【亿元】 -> 转元（与其他源一致）
            out["total_mv"] = pd.to_numeric(df.get("market_cap"), errors="coerce") * 1e8
            out["circ_mv"] = pd.to_numeric(df.get("circulating_market_cap"), errors="coerce") * 1e8
            out["turnover_rate"] = pd.to_numeric(df.get("turnover_ratio"), errors="coerce")
            # 价格 = 总市值 / 总股本（万股 -> 股）
            cap_wan = pd.to_numeric(df.get("capitalization"), errors="coerce")
            out["close"] = out["total_mv"] / (cap_wan * 1e4)
            out["dv_ttm"] = None
            out["name"] = ""
            out["trade_date"] = str(df.get("trade_date", pd.Series([""]))[0])[:10].replace("-", "")

            # 补名称：zzshare 无 name，用 AkShare 股票列表（一次调用 + 缓存）
            try:
                ak = self._akshare()
                nm = ak.stock_info_a_code_name()   # 列: code, name
                if nm is not None and len(nm):
                    nmap = dict(zip(nm.iloc[:, 0].astype(str), nm.iloc[:, 1].astype(str)))
                    out["name"] = out["ts_code"].map(
                        lambda c: nmap.get(str(c).split(".")[0], ""))
            except Exception:                     # noqa: BLE001
                pass

            # 补股息率：优先智兔（全市场一次），其次东财备源
            if os.environ.get("SCREENER_ENRICH_DV", "1") != "0":
                try:
                    zt = self._fetch_zhitu_dividend()
                    if zt is not None and len(zt):
                        dv = zt.set_index("ts_code")["dv_ttm"]
                        out["dv_ttm"] = out["ts_code"].map(dv)
                except Exception:                     # noqa: BLE001
                    pass
                if out["dv_ttm"].notna().sum() == 0:
                    try:
                        em = self._fetch_em_push()
                        if em is not None and len(em) and "dv_ttm" in em.columns:
                            dv = em.set_index("ts_code")["dv_ttm"]
                            out["dv_ttm"] = out["ts_code"].map(dv)
                            nms = em.set_index("ts_code")["name"]
                            out["name"] = out["name"].where(out["name"].astype(bool),
                                                             out["ts_code"].map(nms))
                    except Exception:                 # noqa: BLE001
                        pass

            if self.verbose:
                print(f"[free-backend] 快照来源: zzshare ({len(out)} 行)")
            return out
        except Exception as e:                        # noqa: BLE001
            if self.verbose:
                print(f"[free-backend] zzshare 失败: {str(e)[:60]}")
            return None

    def _fetch_zhitu_dividend(self) -> Optional[pd.DataFrame]:
        """智兔数服 历史累计分红 —— 一次返回全市场【年均股息率】。

        端点: https://api.zhituapi.com/hitc/lsfh?token=XXX
        字段: dm(代码) mc(名称) pergx(年均股息%) totalgx(累计股息%) count(分红次数)
        实测: 500KB+ 全市场；免费 token 可用（体验版 1000 次/分钟）
        """
        token = os.environ.get("ZHITU_TOKEN", "ZHITU_TOKEN_LIMIT_TEST")
        import requests
        from rate_limit import make_session
        s = make_session(force_proxy=False)
        s.headers.update({"User-Agent": "Mozilla/5.0 Chrome/131"})
        try:
            r = s.get("https://api.zhituapi.com/hitc/lsfh",
                      params={"token": token}, timeout=30)
            if r.status_code != 200:
                if self.verbose:
                    print(f"[free-backend] 智兔分红 HTTP {r.status_code}")
                return None
            data = r.json()
            if not isinstance(data, list) or not data:
                return None
            out = pd.DataFrame(data)
            # dm 为 6 位代码，补后缀
            out["ts_code"] = out["dm"].astype(str).map(self._to_ts)
            out = out.rename(columns={"pergx": "dv_ttm"})
            out["dv_ttm"] = pd.to_numeric(out["dv_ttm"], errors="coerce")
            if self.verbose:
                print(f"[free-backend] 股息率来源: 智兔 lsfh ({len(out)} 行)")
            return out[["ts_code", "dv_ttm"]]
        except Exception as e:                        # noqa: BLE001
            if self.verbose:
                print(f"[free-backend] 智兔分红失败: {str(e)[:60]}")
            return None

    def _fetch_em_push(self) -> Optional[pd.DataFrame]:
        """东财 push2 子域名全市场快照（带限流保护）。

        实测：风控是【动态】的，且【立即换域名重试会加重风控】。
        策略：
          1. 域名冷却表：被封的域名直接跳过
          2. 索引退避：失败后等待 2^n 秒
          3. 分页间隔：每页间 0.35s
          4. 不循环扫描所有子域（只试 2-3 个候选）
        """
        import requests
        from rate_limit import (DomainCooldown, backoff_call, throttle,
                                PAGE_INTERVAL)

        cd = DomainCooldown()
        from rate_limit import make_session
        from em_cookie import get_headers
        s = make_session(force_proxy=True)   # 东财 push2 需走系统代理（实测）
        s.headers.update(get_headers(verbose=self.verbose))   # 含反爬 Cookie
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        # f37 = 股息率(%)（探测确认：平安银行 5.22 / 万科 -13.65）
        fields = "f12,f14,f2,f9,f23,f20,f21,f8,f37"

        # 候选域名：push2delay 风控最宽松（实测可用），其余作备份
        hosts = [
            "82.push2delay.eastmoney.com",
            "push2delay.eastmoney.com",
            "82.push2.eastmoney.com",
            "2.push2.eastmoney.com",
        ]

        def fetch_page(host: str, pn: int) -> Optional[dict]:
            throttle(host)
            # np=1 是【分页开关】，缺失时服务器忽略 pn（永远返回首页）
            # 其他参数（po/fltt/invt/fid）不需要，保持最小集减少 502
            r = s.get("https://%s/api/qt/clist/get" % host,
                      params={"pn": pn, "pz": 200, "np": 1,
                              "fs": fs, "fields": fields},
                      timeout=25)
            # 服务端可能返回非 JSON（风控页/网关错误）
            try:
                return r.json()
            except Exception as e:                    # noqa: BLE001
                raise ValueError(
                    f"非 JSON 响应（HTTP {r.status_code}, len={len(r.text)}）: "
                    f"{r.text[:80]!r}"
                ) from e

        good = None
        rows: list = []
        total = 0
        for host in hosts:
            if cd.is_cooling(host):
                if self.verbose:
                    print(f"[free-backend] {host} 冷却中，跳过")
                continue
            try:
                j = backoff_call(lambda h=host: fetch_page(h, 1),
                                 retries=2, domain=host, cooldown=cd,
                                 verbose=self.verbose)
            except Exception as e:                    # noqa: BLE001
                if self.verbose:
                    print(f"[free-backend] {host} 失败: {str(e)[:50]}")
                continue
            d = (j or {}).get("data") or {}
            diff = d.get("diff")
            chunk = list(diff.values()) if isinstance(diff, dict) else (diff or [])
            if not chunk:
                continue
            good = host
            total = d.get("total") or 0
            rows.extend(chunk)
            break
        if good is None:
            return None

        # 分页（带间隔 + 页数上限 + 单页失败重试）
        from rate_limit import MAX_PAGES
        max_pages = MAX_PAGES if MAX_PAGES > 0 else 25
        page_size = 200
        fail_streak = 0
        pn = 2
        while pn <= max_pages and len(rows) < total:
            time.sleep(PAGE_INTERVAL)
            try:
                j = fetch_page(good, pn)
                fail_streak = 0
            except Exception as e:                    # noqa: BLE001
                fail_streak += 1
                if self.verbose:
                    print(f"[free-backend] 第 {pn} 页失败（{fail_streak}）：{str(e)[:40]}")
                if fail_streak >= 2:
                    break                             # 连续两页失败 -> 放弃
                pn += 1                               # 跳过该页继续
                continue
            d = (j or {}).get("data") or {}
            diff = d.get("diff")
            chunk = list(diff.values()) if isinstance(diff, dict) else (diff or [])
            if not chunk:
                break
            rows.extend(chunk)
            if total and len(rows) >= total:
                break
            if len(chunk) < page_size:                # 已到最后一页
                break
            pn += 1

        if not rows:
            return None
        if self.verbose:
            print(f"[free-backend] 快照来源: {good} ({len(rows)}/{total} 行)")

        out = pd.DataFrame([{
            "ts_code": self._to_ts(r.get("f12")),
            "name": r.get("f14"),
            "close": r.get("f2"),
            "pe_ttm": r.get("f9"),
            "pb": r.get("f23"),
            "total_mv": r.get("f20"),
            "circ_mv": r.get("f21"),
            "turnover_rate": r.get("f8"),
            "dv_ttm": r.get("f37"),      # 股息率(%)，探测确认
        } for r in rows])

        # ⚠️ 字段值缩放：东财在不带 fltt 参数时返回 x100 的整数
        #    f2(价格)/f9(PE)/f23(PB) 需 ÷100
        #    f8(换手率) 已是百分数，无需缩放
        #    f20/f21(市值，元) / f37(股息率%) 原值
        for col in ("close", "pe_ttm", "pb"):
            out[col] = pd.to_numeric(out[col], errors="coerce") / 100.0
        for col in ("total_mv", "circ_mv", "turnover_rate", "dv_ttm"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out[out["ts_code"].str.len() >= 9]
        return out

    def _fetch_akshare_spot(self) -> Optional[pd.DataFrame]:
        """降级：AkShare 快照（东财或新浪）。"""
        try:
            ak = self._akshare()
        except ImportError:
            return None
        for fn in ("stock_zh_a_spot_em", "stock_zh_a_spot"):
            f = getattr(ak, fn, None)
            if f is None:
                continue
            try:
                df = f()
                if df is None or not len(df):
                    continue
                def pick(*names):
                    for n in names:
                        for c in df.columns:
                            if n in str(c):
                                return c
                    return None
                c_code = pick("代码", "symbol", "code")
                out = pd.DataFrame()
                out["ts_code"] = df[c_code].map(self._to_ts) if c_code else ""
                out["name"] = df[pick("名称", "name")].astype(str) if pick("名称", "name") else ""
                for tgt, cands in [("close", ("最新价", "现价")), ("pe_ttm", ("市盈率-动态", "市盈率")),
                                   ("pb", ("市净率",)), ("total_mv", ("总市值",)),
                                   ("circ_mv", ("流通市值",)), ("turnover_rate", ("换手率",)),
                                   ("dv_ttm", ("股息率",))]:
                    c = pick(*cands)
                    out[tgt] = pd.to_numeric(df[c], errors="coerce") if c else None
                if self.verbose:
                    print(f"[free-backend] 快照来源: {fn} ({len(out)} 行)")
                return out
            except Exception as e:                    # noqa: BLE001
                if self.verbose:
                    print(f"[free-backend] {fn} 失败: {str(e)[:60]}")
        return None

    # -- 单股财务指标 ---------------------------------------------------
    def fina_indicator(self, ts_code: str, periods: int = 5) -> pd.DataFrame:
        """单股财务指标（近 N 年）。列对齐 Tushare 常用子集。"""
        import sys, os
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from datasource import get

        try:
            r = get("财务指标", ts_code, source="auto")
        except Exception:                             # noqa: BLE001
            return pd.DataFrame()

        rows = []
        for rec in r:
            row = {"ts_code": ts_code}
            for k in ("加权ROE", "毛利率", "净利率", "每股净资产", "资产负债率"):
                v = rec.get(k)
                tushare_key = {
                    "加权ROE": "roe_waa", "毛利率": "grossprofit_margin",
                    "净利率": "netprofit_margin", "每股净资产": "bps",
                    "资产负债率": "debt_to_assets",
                }[k]
                row[tushare_key] = v.value if not v.missing else None
            p = rec.get("加权ROE").period or ""
            row["end_date"] = str(p)[:8] if p else ""
            if row.get("roe_waa") is not None or row.get("grossprofit_margin") is not None:
                rows.append(row)
        return pd.DataFrame(rows)

    # -- 交易日历 -------------------------------------------------------
    def trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        """交易日历。免费源用最近若干交易日的近似。"""
        # 简化：返回最近 30 个自然日中的工作日
        today = _dt.date.today()
        rows = []
        for i in range(30):
            d = today - _dt.timedelta(days=i)
            rows.append({"cal_date": d.strftime("%Y%m%d"),
                         "is_open": 0 if d.weekday() >= 5 else 1})
        return pd.DataFrame(rows)

    # -- 提供者信息 -----------------------------------------------------
    def describe(self) -> str:
        return "free (AkShare/东财+新浪)"


def make_backend(verbose: bool = False) -> FreeScreenerBackend:
    return FreeScreenerBackend(verbose=verbose)
