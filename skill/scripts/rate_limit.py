"""限流保护 — 全局请求节流 + 指数退避 + 域名冷却。

设计目标（防限流）：
  1. **全局节流**：同一域名的最小请求间隔（默认 0.5s）
  2. **指数退避**：失败后 2^n 秒等待，而非立即换域名（避免加重风控）
  3. **域名冷却**：某域名被拒后，冷却 N 秒内不再使用
  4. **探测轻量化**：可用性探测只做一次，不做循环扫描

用法：
    from rate_limit import throttle, backoff_call, DomainCooldown

    # 节流（同域名自动间隔）
    throttle("push2.eastmoney.com")

    # 带退避的调用
    data = backoff_call(lambda: fetch(url), retries=3, domain="push2.eastmoney.com")

    # 域名冷却
    cd = DomainCooldown()
    if cd.is_cooling("82.push2.eastmoney.com"):
        ...  # 跳过
    cd.mark_blocked("82.push2.eastmoney.com")
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 配置（可用环境变量覆盖）
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# 同域名最小请求间隔（秒）
MIN_INTERVAL = _env_float("RATE_LIMIT_MIN_INTERVAL", 0.5)
# 单次退避基础（秒）：2^attempt * BASE
BACKOFF_BASE = _env_float("RATE_LIMIT_BACKOFF_BASE", 2.0)
# 最大退避（秒）
BACKOFF_MAX = _env_float("RATE_LIMIT_BACKOFF_MAX", 30.0)
# 域名被封后的冷却时长（秒）
COOLDOWN_SECONDS = _env_float("RATE_LIMIT_COOLDOWN", 120.0)
# 分页抓取每页间隔（秒）
PAGE_INTERVAL = _env_float("RATE_LIMIT_PAGE_INTERVAL", 0.35)
# 【东财（AkShare *_em 系列）专用间隔】（秒）—— 默认 1s
# 背景：AkShare 的 stock_yjbb_em / stock_fhps_em / stock_zcfz_em 等 *_em
#       接口均直连东财数据中心（datacenter-web.eastmoney.com），且【逐页翻页】
#       都是【真实 HTTP 请求】（非本地分页，pageSize=500，实测
#       yjbb 24 页/次、fhps 8 页/年），无本地缓存。
#       东财对数据中心有频率风控（IP 级），密集翻页易触发限流。
# 取值：1s/页。为什么不取 3s：逐页 3s 下总耗时 ~3.2 分钟（64 页）
#       不可接受；1s/页 下 ~64s，兼顾保护与速度。
# 覆盖：Tier1 的财务（yjbb_em 1 次）+ 分红（fhps_em 5 年）均走此间隔。
EM_MIN_INTERVAL = _env_float("RATE_LIMIT_EM_INTERVAL", 1.0)
# 东财域名标识（throttle 按域名分桶）
EM_DOMAIN = "eastmoney"
# 全市场快照磁盘缓存 TTL（秒）——默认 6 小时，避免同日重复拉取
SNAPSHOT_TTL = _env_float("SCREENER_SNAPSHOT_TTL", 6 * 3600)
# 分页上限（防一次性狂拉；设为 0 表示不限）
MAX_PAGES = int(_env_float("SCREENER_MAX_PAGES", 30))


# ---------------------------------------------------------------------------
# 全局节流
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_last_request: dict[str, float] = {}


def throttle(domain: str, interval: Optional[float] = None) -> float:
    """阻塞直到对该域名的请求满足最小间隔。返回实际等待秒数。"""
    iv = MIN_INTERVAL if interval is None else interval
    if iv <= 0:
        return 0.0
    with _lock:
        now = time.time()
        last = _last_request.get(domain, 0.0)
        wait = iv - (now - last)
        if wait > 0:
            # 先占位再释放锁（避免并发穿透）
            _last_request[domain] = now + wait
        else:
            _last_request[domain] = now
            wait = 0.0
    if wait > 0:
        time.sleep(wait)
    return wait


def reset_throttle(domain: Optional[str] = None) -> None:
    with _lock:
        if domain is None:
            _last_request.clear()
        else:
            _last_request.pop(domain, None)


# ---------------------------------------------------------------------------
# 域名冷却（被风控后暂停使用）
# ---------------------------------------------------------------------------

class DomainCooldown:
    """记录被风控的域名，冷却期内跳过。"""

    _blocked: dict[str, float] = {}
    _l = threading.Lock()

    def mark_blocked(self, domain: str, seconds: Optional[float] = None) -> None:
        sec = COOLDOWN_SECONDS if seconds is None else seconds
        with self._l:
            self._blocked[domain] = time.time() + sec

    def is_cooling(self, domain: str) -> bool:
        with self._l:
            until = self._blocked.get(domain)
            if until is None:
                return False
            if time.time() >= until:
                self._blocked.pop(domain, None)
                return False
            return True

    def remaining(self, domain: str) -> float:
        with self._l:
            until = self._blocked.get(domain, 0.0)
        return max(0.0, until - time.time())

    def clear(self) -> None:
        with self._l:
            self._blocked.clear()

    def status(self) -> dict[str, float]:
        """返回 {域名: 剩余冷却秒数}。"""
        with self._l:
            now = time.time()
            return {d: max(0.0, u - now) for d, u in self._blocked.items() if u > now}


# ---------------------------------------------------------------------------
# 指数退避
# ---------------------------------------------------------------------------

def _is_block_error(exc: Exception) -> bool:
    """判断异常是否像"被风控"（而非服务端/网络故障）。

    注意：5xx（502/503/504）是**服务端故障**，不应触发域冷却
    —— 否则节点抖动会让整个域名被拉黑。
    """
    s = f"{type(exc).__name__}: {exc}".lower()
    # 服务端故障（不冷却）
    if any(k in s for k in ("502", "503", "504", "bad gateway",
                            "service unavailable", "gateway timeout")):
        return False
    keys = ("remote disconnected", "connection aborted", "connection reset",
            "too many requests", "429", "403", "forbidden",
            "max retries", "connectionerror", "eof occurred")
    return any(k in s for k in keys)


def backoff_call(
    fn: Callable[[], Any],
    *,
    retries: int = 3,
    domain: Optional[str] = None,
    cooldown: Optional[DomainCooldown] = None,
    base: Optional[float] = None,
    verbose: bool = False,
) -> Any:
    """带指数退避 + 节流的调用。

    与被限流的"立即换域名重试"相反 —— 本函数会**等待**而不是加急。
    """
    b = BACKOFF_BASE if base is None else base
    cd = cooldown if cooldown is not None else DomainCooldown()
    last: Optional[Exception] = None

    for attempt in range(retries):
        if domain:
            since = cd.remaining(domain)
            if since > 0:
                # 等待冷却结束（不超过本次退避预算）后再试
                if since > BACKOFF_MAX:
                    if verbose:
                        print(f"[rate-limit] {domain} 冷却剩余 {since:.0f}s 过长，放弃")
                    return None
                if verbose:
                    print(f"[rate-limit] {domain} 冷却中，等待 {since:.0f}s")
                time.sleep(since)
            throttle(domain)

        try:
            return fn()
        except Exception as e:                        # noqa: BLE001
            last = e
            blocked = _is_block_error(e)
            if attempt + 1 < retries:
                wait = min(b ** (attempt + 1), BACKOFF_MAX)
                if blocked and domain:
                    # 被拒：冷却时长 = 退避值（不强制最低冷却，否则重试永远等不到）
                    cd.mark_blocked(domain, seconds=wait)
                if verbose:
                    print(f"[rate-limit] 第 {attempt+1} 次失败（{str(e)[:40]}），等待 {wait:.0f}s")
                time.sleep(wait)
            else:
                if blocked and domain:
                    cd.mark_blocked(domain)

    if last is not None:
        raise last
    return None


# ---------------------------------------------------------------------------
# 网络会话（统一禁用系统代理）
# ---------------------------------------------------------------------------

NO_PROXY_HOSTS = (
    "eastmoney.com", "sinajs.cn", "sina.com.cn", "cninfo.com.cn",
    "sse.com.cn", "szse.cn", "10jqka.com.cn", "xueqiu.com",
)


def make_session(disable_proxy: bool = True, force_proxy: bool = False):
    """创建 requests 会话。

    本项目国内财经站的网络策略（实测）：
      - **直连可用**（无代理时）：东财 datacenter/巨潮/新浪均直连正常
      - **但东财 push2 例外**：实测在【全局代理】下直连会 RemoteDisconnected，
        走系统代理才能通（因该域名对直连 IP 有额外风控）。

    参数：
      disable_proxy=True   不读系统代理（默认，适合 datacenter/巨潮等）
      force_proxy=True     强制走系统代理（适合东财 push2 行情）
    """
    import requests
    s = requests.Session()
    if force_proxy and os.environ.get("EM_USE_PROXY", "1") != "0":
        s.trust_env = True          # 读 HTTP_PROXY / 系统代理
        return s
    if disable_proxy:
        s.trust_env = False
        s.proxies = {"http": None, "https": None}
    return s


# ---------------------------------------------------------------------------
# 简易状态报告
# ---------------------------------------------------------------------------

def describe() -> str:
    cd = DomainCooldown()
    st = cd.status()
    lines = [
        f"最小间隔: {MIN_INTERVAL}s | 退避基础: {BACKOFF_BASE}s | 冷却: {COOLDOWN_SECONDS}s",
        f"分页间隔: {PAGE_INTERVAL}s | 分页上限: {MAX_PAGES}（0=不限）",
        f"快照缓存 TTL: {SNAPSHOT_TTL:.0f}s",
    ]
    if st:
        lines.append("冷却中的域名: " + ", ".join(f"{d}({v:.0f}s)" for d, v in st.items()))
    else:
        lines.append("冷却中的域名: 无")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
