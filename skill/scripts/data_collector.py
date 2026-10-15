#!/usr/bin/env python
"""data_collector.py — 统一数据采集入口（替代 tushare_collector.py）。

按 router 的逐段路由表取数，每个源按优先级尝试，失败自动降级。
输出 data_pack_market.md，每段标注实际来源。

用法：
    python scripts/data_collector.py --code 600887.SH
    python scripts/data_collector.py --code 600887.SH --output output/600887_伊利股份/data_pack_market.md
    python scripts/data_collector.py --code 600887.SH --sources akshare,tushare   # 限定源
    python scripts/data_collector.py --code 600887.SH --list-sources             # 查看可用源

设计：
    业务代码 → data_collector → datasource.get() → 各 adapter
                                        ↑
                            切源只改 router.py，本文件不动
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from datasource import get, list_sources, DataSourceError          # noqa: E402
from datasource.router import (                                      # noqa: E402
    SectionResult, dataset_for, is_degradable, sources_for, title_for,
)


def collect_section(section: str, code: str, sources: list[str] | None = None) -> tuple[SectionResult, list[dict]]:
    """取一个数据段，按优先级尝试各源。"""
    ds_name = dataset_for(section)
    res = SectionResult(section=section, dataset=ds_name or "-", ok=False,
                        degradable=is_degradable(section))

    if ds_name is None:
        res.attempts.append(("router", f"段 {section} 无数据集映射（需 Tushare 或本地计算）"))
        return res, []

    candidate = sources or sources_for(section)
    for src in candidate:
        try:
            r = get(ds_name, code, source=src)
            if len(r) > 0:
                res.ok = True
                res.source = src
                res.rows = len(r)
                return res, [rec.to_dict() for rec in r]
            res.attempts.append((src, "返回空"))
        except Exception as e:                      # noqa: BLE001
            res.attempts.append((src, f"{type(e).__name__}: {e}"))
    return res, []


def _fmt_val(v: dict) -> str:
    """把 Value dict 格式化为表格单元格。"""
    txt = v.get("text")
    if txt:
        return str(txt)
    val = v.get("value")
    if val is None:
        return "⚠️"
    return f"{val:,.2f}" if abs(val) < 1e15 else str(val)


def section_markdown(section: str, res: SectionResult, rows: list[dict]) -> str:
    """把一段结果渲染为 markdown（行为记录，字段为列）。"""
    lines = [f"## {title_for(section)}", ""]

    if not res.ok:
        tag = "⚠️ 降级" if res.degradable else "❌ 采集失败"
        lines += [f"> {tag}：{res.reason()}", ""]
        if not rows:
            lines += ["（本段数据不可用）", ""]
            return "\n".join(lines)

    if not rows:
        return "\n".join(lines)

    # 财务类只保留年报（12-31），避免混入季报
    def _is_annual(r: dict) -> bool:
        for k in ("报告期", "交易日期", "曲线日期"):
            v = r.get(k, {})
            p = (v.get("text") if isinstance(v, dict) else None) or ""
            if not p:
                continue
            p8 = str(p).replace("-", "")[:8]
            if len(p8) >= 8:
                return p8[4:8] == "1231"
        return True

    if res.dataset in ("利潦表", "利润表", "资亱负债表", "现金流量表", "财务指标", "分红"):
        annual = [r for r in rows if _is_annual(r)]
        if annual:
            rows = annual
        rows = rows[:8]          # 最多 8 年
    else:
        rows = rows[:60]
    res.rows = len(rows)

    lines += [f"> **数据来源**: `{res.source}` · {res.rows} 行", ""]

    keys = [k for k in rows[0].keys() if not k.startswith("_")]
    period_keys = [k for k in keys if k in ("报告期", "交易日期", "曲线日期", "公告日")]
    keys = period_keys + [k for k in keys if k not in period_keys]

    lines.append("| " + " | ".join(keys) + " |")
    lines.append("|" + "---|" * len(keys))
    for r in rows:
        lines.append("| " + " | ".join(_fmt_val(r.get(k, {})) for k in keys) + " |")
    lines.append("")
    return "\n".join(lines)


def build_header(code: str, results: list[SectionResult]) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    used = sorted({r.source for r in results if r.ok and r.source})
    degraded = [r.section for r in results if not r.ok and r.degradable]
    failed = [r.section for r in results if not r.ok and not r.degradable]

    lines = [
        f"# 数据包 — {code}",
        "",
        f"*生成时间: {now}*",
        f"*采集器: data_collector.py（数据层路由）*",
        f"*实际使用源: {', '.join(used) or '无'}*",
    ]
    if degraded:
        lines.append(f"*⚠️ 降级段（可容忍缺失）: §{', §'.join(degraded)}*")
    if failed:
        lines.append(f"*❌ 失败段（必需）: §{', §'.join(failed)}*")
    lines += ["", "---", ""]
    return "\n".join(lines)


def build_summary(results: list[SectionResult]) -> str:
    lines = ["", "---", "", "## 采集汇总", "",
             "| 段 | 数据集 | 来源 | 行数 | 状态 |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| §{r.section} | {r.dataset} | {r.source or '-'} | {r.rows} | {r.status} |")
    ok = sum(1 for r in results if r.ok)
    lines += ["", f"**{ok}/{len(results)} 段采集成功。**", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="龟龟框架统一数据采集（数据层路由版）")
    ap.add_argument("--code", required=False, help="股票代码，如 600887.SH")
    ap.add_argument("--output", default=None, help="输出路径")
    ap.add_argument("--sources", default=None, help="限定数据源，逗号分隔（默认按 router 优先级）")
    ap.add_argument("--sections", default=None, help="限定段号，逗号分隔，如 1,3,4,5")
    ap.add_argument("--list-sources", action="store_true", help="列出可用数据源后退出")
    args = ap.parse_args()

    if args.list_sources:
        print("已注册:", list_sources())
        print("当前可用:", list_sources(only_available=True))
        return 0

    if not args.code:
        ap.error("--code 是必需的")

    code = args.code
    limit_sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
    sections = ([s.strip() for s in args.sections.split(",")] if args.sections
                else ["1", "2", "3", "3P", "4", "4P", "5", "6", "11", "12", "14", "15", "16"])

    print(f"采集 {code} — {len(sections)} 段")
    print(f"可用源: {list_sources(only_available=True)}")

    results: list[SectionResult] = []
    parts: list[str] = []
    for sec in sections:
        res, rows = collect_section(sec, code, limit_sources)
        results.append(res)
        print(f"  §{sec:<4} {res.dataset:<12} {res.status}")
        if res.reason() and not res.ok:
            print(f"         ↳ {res.reason()}")
        parts.append(section_markdown(sec, res, rows))

    doc = build_header(code, results) + "\n".join(parts) + build_summary(results)

    out = args.output
    if not out:
        safe = code.replace(".", "_")
        out = str(_HERE.parent / "output" / safe / "data_pack_market.md")
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc, encoding="utf-8")
    print(f"\n已写入: {p}")

    failed_req = [r.section for r in results if not r.ok and not r.degradable]
    if failed_req:
        print(f"⚠️ 必需段失败: §{', §'.join(failed_req)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
