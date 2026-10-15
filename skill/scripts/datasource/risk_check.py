"""§13 风险警示 — 本地确定性检测（无 LLM 判断）。

与 tushare_modules/assembly.py 的 RiskWarning 类逻辑对齐：
  - YoY 异常（>300%）
  - 财务数据缺失
  - 审计意见风险
  - 商誉占总资产比例
  - 资产负债率

输出格式：
### 13.1 脚本自动检测
**高风险:**
- [CODE] 描述
**中风险:**
- ...
"""

from __future__ import annotations

from typing import Any

# 阈值（与 assembly.py 保持一致）
YOY_THRESHOLD = 300.0          # %
GOODWILL_RATIO_HIGH = 30.0     # %
DEBT_RATIO_HIGH = 70.0         # %
PLEDGE_RATIO_HIGH = 70.0       # %
PLEDGE_RATIO_MID = 50.0        # %

# 参与 YoY 扫描的字段（业务名）
YOY_FIELDS = {
    "利润表": ["营业收入", "归母净利润"],
    "现金流量表": ["经营活动现金流"],
}

# YoY 扫描只覆盖近 N 年（框架分析范围，避免历史异常干扰）
YOY_LOOKBACK = 6


def _num(v: Any) -> float | None:
    if isinstance(v, dict):
        x = v.get("value")
        return float(x) if isinstance(x, (int, float)) else None
    return float(v) if isinstance(v, (int, float)) else None


def check_yoy(sections: dict[str, list[dict]]) -> list[tuple[str, str]]:
    """YoY 异常检测。只比较**年报**（避免年度/季度混比造成假阳）。

    sections: {数据集名: [行...]}
    """
    out: list[tuple[str, str]] = []
    for ds, fields in YOY_FIELDS.items():
        rows = _annual_only(sections.get(ds) or [])[:YOY_LOOKBACK]
        if len(rows) < 2:
            continue
        for f in fields:
            for i in range(len(rows) - 1):
                cur, prv = _num(rows[i].get(f, {})), _num(rows[i + 1].get(f, {}))
                if cur is None or prv is None or abs(prv) < 1e-6:
                    continue
                yoy = abs((cur - prv) / abs(prv)) * 100
                if yoy > YOY_THRESHOLD:
                    out.append(("高", f"[YOY_ANOMALY] {ds}/{f}: 同比变化 {yoy:.0f}% 超过 {YOY_THRESHOLD:.0f}% 阈值"))
    return out


def _annual_only(rows: list[dict]) -> list[dict]:
    """只保留年报行（period 为 YYYY1231 且该 period 在年报集合中）。

    AkShare/新浪财报表会同时返回年报和季报，不能只看后缀。
    策略：只保留 period 形如 YYYY1231 且各年只出现一次的行。
    """
    seen: dict[str, dict] = {}
    for r in rows:
        p = ""
        for v in r.values():
            if isinstance(v, dict) and v.get("period"):
                p = str(v["period"]).replace("-", "")[:8]
                break
        if len(p) == 8 and p[4:8] == "1231" and p not in seen:
            seen[p] = r
    # 按期倒序（最新在前）
    return [seen[k] for k in sorted(seen, reverse=True)]


def check_missing(sections: dict[str, list[dict]], required: dict[str, list[str]]) -> list[tuple[str, str]]:
    """必需字段缺失检测。"""
    out: list[tuple[str, str]] = []
    for ds, fields in required.items():
        rows = sections.get(ds) or []
        if not rows:
            continue
        for f in fields:
            vals = [_num(r.get(f, {})) for r in rows]
            if all(v is None for v in vals):
                out.append(("中", f"[MISSING_DATA] {ds}/{f}: 全部期数缺失"))
    return out


def check_goodwill(sections: dict[str, list[dict]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    rows = sections.get("资产负债表") or []
    if not rows:
        return out
    gw = _num(rows[0].get("商誉", {}))
    ta = _num(rows[0].get("总资产", {}))
    if gw and ta and ta > 0:
        r = gw / ta * 100
        if r > GOODWILL_RATIO_HIGH:
            out.append(("高", f"[GOODWILL] 商誉占总资产 {r:.1f}% 超过 {GOODWILL_RATIO_HIGH:.0f}%"))
        elif r > 10:
            out.append(("中", f"[GOODWILL] 商誉占总资产 {r:.1f}%"))
    return out


def check_debt(sections: dict[str, list[dict]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    rows = sections.get("资产负债表") or []
    if not rows:
        return out
    tl = _num(rows[0].get("总负债", {}))
    ta = _num(rows[0].get("总资产", {}))
    if tl and ta and ta > 0:
        r = tl / ta * 100
        if r > DEBT_RATIO_HIGH:
            out.append(("高", f"[DEBT] 资产负债率 {r:.1f}% 超过 {DEBT_RATIO_HIGH:.0f}%"))
        elif r > 60:
            out.append(("中", f"[DEBT] 资产负债率 {r:.1f}%"))
    return out


def check_pledge(sections: dict[str, list[dict]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    rows = sections.get("股权质押") or []
    for r in rows:
        p = _num(r.get("质押比例", {}))
        if p is None:
            continue
        if p > PLEDGE_RATIO_HIGH:
            out.append(("高", f"[PLEDGE] 股权质押比例 {p:.2f}% 超过 {PLEDGE_RATIO_HIGH:.0f}%"))
        elif p > PLEDGE_RATIO_MID:
            out.append(("中", f"[PLEDGE] 股权质押比例 {p:.2f}%"))
    return out


def render(sections: dict[str, list[dict]], required: dict[str, list[str]] | None = None) -> str:
    """生成 §13 markdown。"""
    items: list[tuple[str, str]] = []
    items += check_yoy(sections)
    items += check_missing(sections, required or {})
    items += check_goodwill(sections)
    items += check_debt(sections)
    items += check_pledge(sections)

    lines = ["### 13.1 脚本自动检测", ""]
    if not items:
        lines += ["无异常检测项。", ""]
        return "\n".join(lines)

    for sev in ("高", "中", "低"):
        group = [m for s, m in items if s == sev]
        if group:
            lines.append(f"**{sev}风险:**")
            lines += [f"- {m}" for m in group]
            lines.append("")
    lines += ["### 13.2 Agent WebSearch 补充", "",
              "| # | 类型 | 严重程度 | 描述 |", "|---|------|---------|------|",
              "| — | （待 Agent 补充） | — | 治理/诉讼/行业政策等非结构化风险 |", ""]
    return "\n".join(lines)
