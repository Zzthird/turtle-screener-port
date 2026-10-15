#!/usr/bin/env python
"""data_collector.py — 统一数据采集入口（多源路由）。

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
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from datasource import get, list_sources, DataSourceError          # noqa: E402
from datasource.router import (                                      # noqa: E402
    SectionResult, dataset_for, is_degradable, sources_for, title_for,
)

# §13 风险检测的必需字段（用于缺失检测）
REQUIRED_FIELDS: dict[str, list[str]] = {
    "利润表": ["营业收入", "归母净利润"],
    "现金流量表": ["经营活动现金流"],
    "资产负债表": ["总资产", "总负债"],
}


def _collect_parent(section: str, code: str, res: SectionResult) -> tuple[SectionResult, list[dict]]:
    """§3P / §4P：从巨潮年报 PDF 提取母公司报表。

    流程：找 PDF（已有则用）→ 提取 → 校验（母公司 <= 合并）
    """
    num = code.split(".")[0]
    roots = [Path(_HERE).resolve().parent.parent / "output",
             Path.cwd() / "output",
             Path.cwd()]
    pdf: Path | None = None
    for root in roots:
        if not root.exists():
            continue
        for pat in (f"{num}*/{num}_*年报*.pdf", f"{num}*/*.pdf", f"*{num}*.pdf"):
            cands = sorted(root.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True)
            if cands:
                pdf = cands[0]
                break
        if pdf:
            break

    if pdf is None:
        res.attempts.append(("cninfo", "未找到年报 PDF（先用 cninfo_downloader.py 下载）"))
        return res, []

    try:
        from pdf_parent_extractor import extract_parent_reports, validate, to_rows
    except ImportError as e:                        # noqa: BLE001
        res.attempts.append(("pdf", f"模块缺失：{e}"))
        return res, []

    try:
        reps = extract_parent_reports(str(pdf))
    except Exception as e:                          # noqa: BLE001
        res.attempts.append(("pdf", f"提取失败：{type(e).__name__}: {e}"))
        return res, []

    if not reps:
        res.attempts.append(("pdf", "未找到母公司报表章节"))
        return res, []

    warns = validate(reps)
    key = "母公司利润表" if section == "3P" else "母公司资产负债表"
    rep = reps.get(key)
    rows = to_rows(reps).get(key, [])

    if not rows:
        res.attempts.append(("pdf", f"{key} 提取为空"))
        return res, []

    res.dataset = key              # 供渲染层选择矩阵格式
    res.ok = True
    res.source = "cninfo_pdf"
    res.rows = len(rows)
    res.attempts.extend([("validate", w) for w in warns])
    return res, rows


def collect_section(section: str, code: str, sources: list[str] | None = None) -> tuple[SectionResult, list[dict]]:
    """取一个数据段，按优先级尝试各源。"""
    ds_name = dataset_for(section)
    res = SectionResult(section=section, dataset=ds_name or "-", ok=False,
                        degradable=is_degradable(section))

    if ds_name is None:
        # 母公司报表（§3P/§4P）：走 PDF 提取
        if section in ("3P", "4P"):
            return _collect_parent(section, code, res)
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


def _fmt_num(v: float, unit: str = "") -> str:
    """格式化数值。金额转百万元（框架统一口径）。"""
    if v is None:
        return "—"
    if unit == "元":
        v = v / 1e6                      # 元 -> 百万元
    elif unit == "股":
        v = v / 1e6                      # 股 -> 百万股
    if abs(v) >= 1e6:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:,.2f}"
    return f"{v:,.4f}".rstrip("0").rstrip(".") or "0"


def _fmt_val(v: dict) -> str:
    """把 Value dict 格式化为表格单元格。"""
    if not isinstance(v, dict):
        return "—"
    txt = v.get("text")
    if txt:
        return str(txt)
    val = v.get("value")
    if val is None:
        return "—"
    return _fmt_num(val, v.get("unit", ""))


def _period_label(v, r: dict | None = None) -> str:
    """提取期间标签（如 20241231 -> 2024）。"""
    p = ""
    if isinstance(v, dict):
        p = v.get("period") or v.get("text") or ""
    p = str(p).replace("-", "").replace(" 00:00:00", "").strip()
    if len(p) >= 8 and p[:8].isdigit():
        return p[:4]
    if len(p) == 4 and p.isdigit():
        return p
    return p[:10] or "?"


def _row_period(r: dict) -> str:
    """从一行记录推断期间。优先取任一字段的 period。"""
    for key in ("报告期", "交易日期", "曲线日期", "公告日"):
        if key in r:
            p = _period_label(r[key])
            if p and p != "?":
                return p
    for v in r.values():
        if isinstance(v, dict) and v.get("period"):
            p = _period_label(v)
            if p and p != "?":
                return p
    return "?"


# 矩阵型渲染（字段为行、期间为列）的数据集
MATRIX_DATASETS = {"利润表", "资产负债表", "现金流量表", "财务指标",
                   "母公司利润表", "母公司资产负债表", "母公司现金流量表"}


def _render_matrix(res: SectionResult, rows: list[dict]) -> list[str]:
    """字段为行、期间为列（与 Tushare 版 datapack 对齐）。"""
    periods: list[str] = []
    idx: dict[str, dict] = {}
    for r in rows:
        p = _row_period(r)
        if p == "?":
            continue
        if p not in periods:
            periods.append(p)
        idx.setdefault(p, r)
    if not periods:
        return []

    keys = [k for k in rows[0].keys()
            if not k.startswith("_") and k not in ("报告期", "交易日期", "曲线日期")]

    out = ["| 项目（百万元） | " + " | ".join(periods) + " |",
           "|---|" + "---|" * len(periods)]
    for k in keys:
        cells = [_fmt_val(idx.get(p, {}).get(k, {})) for p in periods]
        if all(c == "—" for c in cells):
            continue                      # 全空的字段不输出
        out.append(f"| {k} | " + " | ".join(cells) + " |")
    out.append("")
    return out


# 列表型数据集（每行一个实体，如股东/业务分部）
LIST_DATASETS = {"十大股东", "主营业务构成", "分红", "股份回购", "股权质押",
                 "日线行情", "周线行情", "无风险利率"}


def _render_list(res: SectionResult, rows: list[dict]) -> list[str]:
    """每行一个记录，字段为列。"""
    keys = [k for k in rows[0].keys() if not k.startswith("_") and k != "报告期"]
    # 关键列优先（年度必须在第一列，quality_control 依赖）
    priority = ["年度", "报告期", "交易日期", "曲线日期", "公告日", "公告日期",
                "股东名", "业务项目", "回购进度", "质押比例", "每股股息", "分红进度"]
    keys = [k for k in priority if k in keys] + [k for k in keys if k not in priority]

    out = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    for r in rows:
        out.append("| " + " | ".join(_fmt_val(r.get(k, {})) for k in keys) + " |")
    out.append("")
    return out


def _render_keyvalue(res: SectionResult, rows: list[dict]) -> list[str]:
    """键值表（如 §1 基本信息）。"""
    out = []
    for r in rows:
        for k, v in r.items():
            if k.startswith("_") or k == "报告期":
                continue
            cell = _fmt_val(v)
            if cell == "—":
                continue
            out.append(f"| {k} | {cell} |")
    if out:
        out = ["| 项目 | 数值 |", "|---|---|"] + out[:40]
    out.append("")
    return out


def _aggregate_dividends(rows: list[dict], all_sections: dict) -> list[dict]:
    """§6 分红按年聚合：{年度, 总分红(百万元), 每股股息(元/股), 分红次数}。

    quality_control.py 期望格式：第一列是 4 位年度，含「总分红」列（百万元）。
    总分红 = 每股股息(元/股) × 总股本(股) / 1e6
    """
    shares = None
    for r in (all_sections.get("资产负债表") or []):
        v = r.get("总股本", {})
        if isinstance(v, dict) and v.get("value"):
            shares = v["value"]
            break

    agg: dict[str, dict] = {}
    for r in rows:
        # 年份取【公告日】的 period（适配器已用公告年推算财年），
        # 不能用除权日——其 period 在 mapper 中会被 end_date 回退覆盖
        yr = ""
        for k in ("报告期", "公告日"):
            v = r.get(k, {})
            if isinstance(v, dict) and v.get("period"):
                m = re.search(r"(20\d{2})", str(v["period"]))
                if m:
                    yr = m.group(1)
                    break
        # 回退：从任意字段的 period 推
        if not yr:
            for v in r.values():
                if isinstance(v, dict) and v.get("period"):
                    m = re.search(r"(20\d{2})", str(v["period"]))
                    if m:
                        yr = m.group(1)
                        break
        if not yr:
            continue
        dps = r.get("每股股息", {})
        raw = dps.get("value") if isinstance(dps, dict) else None
        if raw is None:
            continue
        dps_v = raw / 10.0          # ⚠️ AkShare「派息」= 每 10 股派 X 元 -> 每股权
        slot = agg.setdefault(yr, {"总分红": 0.0, "每股股息": 0.0, "分红次数": 0})
        slot["每股股息"] += dps_v
        slot["分红次数"] += 1

    out = []
    for yr in sorted(agg, reverse=True):
        s = agg[yr]
        total_mm = (s["每股股息"] * shares / 1e6) if shares else s["每股股息"]
        out.append({
            "年度": {"text": yr, "value": None, "period": yr + "1231"},
            # 已经是百万元，标 unit="百万元" 避免渲染层再除 1e6
            "总分红": {"value": total_mm, "unit": "百万元"},
            "每股股息": {"value": s["每股股息"], "unit": "元/股"},
            "分红次数": {"value": float(s["分红次数"]), "unit": "pct"},
        })
    return out


def section_markdown(section: str, res: SectionResult, rows: list[dict]) -> str:
    """把一段结果渲染为 markdown。"""
    lines = [f"## {title_for(section)}", ""]

    if not res.ok:
        tag = "⚠️ 降级" if res.degradable else "❌ 采集失败"
        lines += [f"> {tag}：{res.reason()}", ""]
        lines += ["（本段数据不可用）", ""]
        return "\n".join(lines)

    if not rows:
        lines += ["（无数据）", ""]
        return "\n".join(lines)

    # 财务类只保留年报（12-31），避免混入季报
    def _is_annual(r: dict) -> bool:
        p = _row_period(r)
        return True if p == "?" else p.isdigit()

    if res.dataset in MATRIX_DATASETS | {"分红"}:
        annual = [r for r in rows if _is_annual(r)]
        if annual:
            rows = annual
        rows = rows[:8]          # 最多 8 年
    elif res.dataset in ("周线行情", "无风险利率", "分红"):
        rows = rows[:20]
    else:
        rows = rows[:60]
    res.rows = len(rows)

    lines += [f"> **数据来源**: `{res.source}` · {res.rows} 行", ""]

    if res.dataset in MATRIX_DATASETS:
        lines += _render_matrix(res, rows)
    elif res.dataset in LIST_DATASETS:
        lines += _render_list(res, rows)
    else:
        lines += _render_keyvalue(res, rows)
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
                else ["1", "2", "3", "3P", "4", "4P", "5", "6", "7", "9", "11", "12", "13", "14", "15", "16"])

    print(f"采集 {code} — {len(sections)} 段")
    print(f"可用源: {list_sources(only_available=True)}")

    results: list[SectionResult] = []
    parts: list[str] = []
    raw_sections: dict[str, list[dict]] = {}
    pending_13 = False

    # 数据段先采集（§13 依赖前面的结果）
    for sec in sections:
        if sec == "13":
            pending_13 = True
            continue
        res, rows = collect_section(sec, code, limit_sources)
        results.append(res)
        print(f"  §{sec:<4} {res.dataset:<12} {res.status}")
        if res.reason() and not res.ok:
            print(f"         ↳ {res.reason()}")
        if rows and res.dataset not in ("-", None):
            raw_sections.setdefault(res.dataset, rows)
        # §6 分红：按年聚合（quality_control 需要「年度+总分红」）
        if sec == "6" and rows:
            rows = _aggregate_dividends(rows, raw_sections)
        parts.append(section_markdown(sec, res, rows))

    # §13 本地风险检测（基于已采集数据）
    if pending_13:
        from datasource import risk_check
        body = risk_check.render(raw_sections, REQUIRED_FIELDS)
        parts.append(f"## {title_for('13')}\n\n{body}")
        n_items = body.count("\n- ") + body.count("\n- ")
        res13 = SectionResult(section="13", dataset="风险检测", ok=True,
                              source="local", rows=len(raw_sections))
        results.append(res13)
        print(f"  §13  风险检测       ✅ local（扫 {len(raw_sections)} 个数据集）")

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
