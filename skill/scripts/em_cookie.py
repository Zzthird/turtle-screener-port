"""EastMoney push2 反爬 Cookie 自动获取 + 缓存。

实测（2026-10）：
  东财 push2 需要浏览器 Cookie（关键是 st_pvi / st_nvi 反爬令牌），
  与 IP / 代理 / TLS 指纹均无关。缺失 Cookie 时返回 RemoteDisconnected。

  获取方式：先访问东财任一页面（如 quote.eastmoney.com），
  服务端会 Set-Cookie 下发 st_pvi / st_nvi。

缓存策略：
  Cookie 写到 output/.em_cookie.json，TTL 默认 6 小时。
  过期后自动重新获取。
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 拿 Cookie 的入口页（任一东财页面均可）
COOKIE_SEED_URLS = [
    "https://quote.eastmoney.com/center/gridlist.html",
    "https://www.eastmoney.com/",
    "https://quote.eastmoney.com/",
]

DEFAULT_TTL = 6 * 3600


def _cache_path() -> str:
    d = os.environ.get("EM_COOKIE_CACHE_DIR") or os.path.join("output", ".em_cache")
    return os.path.join(d, "cookie.json")


def _load_cached(ttl: int) -> Optional[dict]:
    p = _cache_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - d.get("ts", 0) > ttl:
            return None
        ck = d.get("cookie") or ""
        # 关键令牌必须存在
        if "st_pvi" not in ck and "st_nvi" not in ck:
            return None
        return d
    except Exception:                                 # noqa: BLE001
        return None


def _save_cache(cookie: str, raw: dict) -> None:
    p = _cache_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "cookie": cookie, "raw": raw}, f)
    except Exception:                                 # noqa: BLE001
        pass


def fetch_cookie(force: bool = False, ttl: int = DEFAULT_TTL,
                 verbose: bool = False) -> str:
    """获取东财 Cookie（优先级：缓存 > 手动种子文件 > 自动获取）。

    手动种子：output/.em_cache/cookie.txt
      用户从浏览器 F12 复制 Cookie 粘入即可（最可靠）。
    """
    if not force:
        c = _load_cached(ttl)
        if c:
            if verbose:
                print("[em-cookie] 使用缓存 Cookie")
            return c["cookie"]

    # 手动种子文件（用户提供）
    seed = os.path.join(os.path.dirname(_cache_path()), "cookie.txt")
    if os.path.exists(seed):
        try:
            with open(seed, encoding="utf-8") as f:
                ck = f.read().strip()
            if ck and ("st_pvi" in ck or "st_nvi" in ck):
                _save_cache(ck, {})
                if verbose:
                    print(f"[em-cookie] 使用手动种子（{len(ck)} 字符）")
                return ck
            elif verbose:
                print("[em-cookie] 种子文件存在但缺关键令牌（st_pvi/st_nvi）")
        except Exception as e:                        # noqa: BLE001
            if verbose:
                print(f"[em-cookie] 种子读取失败: {str(e)[:50]}")

    import requests
    from rate_limit import make_session
    s = make_session()
    s.headers.update({"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8",
                      "Accept-Language": "zh-CN,zh;q=0.9"})

    jar: dict[str, str] = {}
    for url in COOKIE_SEED_URLS:
        try:
            r = s.get(url, timeout=20)
            for k, v in s.cookies.get_dict().items():
                jar[k] = v
            if any(k in jar for k in ("st_pvi", "st_nvi")):
                break
        except Exception as e:                        # noqa: BLE001
            if verbose:
                print(f"[em-cookie] {url} 失败: {str(e)[:50]}")
            continue

    cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
    if cookie:
        _save_cache(cookie, jar)
        if verbose:
            print(f"[em-cookie] 已获取 {len(jar)} 个 Cookie: {list(jar)[:6]}")
    elif verbose:
        print("[em-cookie] 未获取到 Cookie")
    return cookie


def get_headers(verbose: bool = False) -> dict:
    """返回带 Cookie 的请求头（供 push2 使用）。"""
    ck = fetch_cookie(verbose=verbose)
    h = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://quote.eastmoney.com/center/gridlist.html",
        "Origin": "https://quote.eastmoney.com",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }
    if ck:
        h["Cookie"] = ck
    return h


if __name__ == "__main__":
    import sys
    v = "-v" in sys.argv
    c = fetch_cookie(force="--force" in sys.argv, verbose=True)
    print("Cookie 长度:", len(c))
    print("内容:", c[:200])
