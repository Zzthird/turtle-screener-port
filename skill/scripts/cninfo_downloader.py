"""巨潮年报下载器 — 从 cninfo 获取年报 PDF。

实测（2026-10）：
  查询接口 http://www.cninfo.com.cn/new/hisAnnouncement/query   ✅ 可用
  下载地址 http://static.cninfo.com.cn/{adjunctUrl}              ✅ 可用
  category_ndbg_szsh = 年报
"""

from __future__ import annotations

import os
import time
from typing import Optional

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
STATIC_BASE = "http://static.cninfo.com.cn/"

# 交易所前缀映射
_PLATE = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

# orgId 缓存（实测：无法用公式推导，必须查）
_ORG_CACHE: dict[str, str] = {}


class CninfoError(Exception):
    pass


def _session():
    from rate_limit import make_session
    s = make_session()                      # 直连：国内财经站不走系统代理
    s.headers.update({
        "User-Agent": _UA,
        "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    return s


def resolve_org_id(num: str, session=None) -> str:
    """查巨潮 orgId。实测无法用公式推导（如 600519->gssh0600519 但 601088->9900003701）。"""
    if num in _ORG_CACHE:
        return _ORG_CACHE[num]
    s = session or _session()
    try:
        r = s.post(SEARCH_URL, data={"keyWord": num, "maxNum": 10}, timeout=25)
        j = r.json()
        for x in (j if isinstance(j, list) else []):
            if str(x.get("code")) == num:
                org = x.get("orgId") or ""
                if org:
                    _ORG_CACHE[num] = org
                    return org
    except Exception:                               # noqa: BLE001
        pass
    return ""


def query_annual_reports(code: str, year: int, page_size: int = 30) -> list[dict]:
    """查询年报公告列表。code 如 600887.SH。返回 [{title, url, date}]。"""
    num, _, mkt = code.partition(".")
    mkt = mkt.upper() or ("SH" if num.startswith("6") else "SZ")
    # column 按交易所：上交所 sse / 深交所 szse / 北交所 bjse
    col_base = {"SH": "sse", "SZ": "szse", "BJ": "bjse"}.get(mkt, "szse")
    column = col_base

    s = _session()
    org = resolve_org_id(num, s)
    stock = f"{num},{org}" if org else num

    # 年报在【次年】披露：2024 年报 -> 2025-01-01~2025-12-31
    payload = {
        "pageNum": 1, "pageSize": page_size,
        "column": column, "tabName": "fulltext",
        "plate": "", "stock": stock,
        "searchkey": "", "secid": "",
        "category": "category_ndbg_szsh",
        "trade": "", "seDate": f"{year+1}-01-01~{year+1}-12-31",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    try:
        r = s.post(QUERY_URL, data=payload, timeout=30)
        j = r.json()
    except Exception as e:                          # noqa: BLE001
        raise CninfoError(f"巨潮查询失败：{type(e).__name__}: {e}") from e

    out: list[dict] = []
    for a in (j.get("announcements") or []):
        title = (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
        # 排除摘要/英文版/已取消
        if any(k in title for k in ["摘要", "英文", "已取消", "公告"]):
            continue
        # 标题形式兼容「2024年年度报告」/「2024年度报告」
        if str(year) not in title:
            continue
        if not any(k in title for k in ["年度报告", "年报"]):
            continue
        out.append({
            "title": title,
            "url": STATIC_BASE + (a.get("adjunctUrl") or ""),
            "date": (a.get("announcementTime") or ""),
        })
    return out


def download_pdf(url: str, dest: str, retries: int = 3) -> str:
    """下载 PDF 到 dest（若已存在且 >100KB 则跳过）。"""
    if os.path.exists(dest) and os.path.getsize(dest) > 100_000:
        return dest
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    s = _session()
    last: Optional[Exception] = None
    for i in range(retries):
        try:
            r = s.get(url, timeout=180, stream=True)
            r.raise_for_status()
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            if os.path.getsize(tmp) < 100_000:
                raise CninfoError("下载内容过小，可能不是 PDF")
            os.replace(tmp, dest)
            return dest
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise CninfoError(f"下载失败（{retries} 次）：{last}")


def fetch_annual_report(code: str, year: int, save_dir: str) -> str:
    """一站式：查最新年报 → 下载。返回 PDF 路径。

    Args:
        code: 如 600887.SH
        year: 财年（如 2024）
        save_dir: 保存目录
    """
    items = query_annual_reports(code, year)
    if not items:
        raise CninfoError(f"未找到 {code} {year} 年报")
    item = items[0]
    fname = f"{code.split('.')[0]}_{year}_年报.pdf"
    dest = os.path.join(save_dir, fname)
    return download_pdf(item["url"], dest)


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="巨潮年报下载器")
    ap.add_argument("--code", required=True, help="股票代码，如 600887.SH")
    ap.add_argument("--year", type=int, required=True, help="财年，如 2024")
    ap.add_argument("--save-dir", default=None)
    ap.add_argument("--query-only", action="store_true", help="只查询不下载")
    args = ap.parse_args()

    save_dir = args.save_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", f"{args.code.split('.')[0]}_report")

    if args.query_only:
        for it in query_annual_reports(args.code, args.year):
            print(f"  {it['title']}\n    {it['url']}")
        sys.exit(0)

    try:
        p = fetch_annual_report(args.code, args.year, save_dir)
        print(f"已下载: {p}  ({os.path.getsize(p)/1e6:.1f} MB)")
    except CninfoError as e:
        print(f"失败: {e}", file=sys.stderr)
        sys.exit(1)
