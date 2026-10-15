#!/usr/bin/env python
"""§17 衍生指标预计算（本地，从 datapack 解析）。

作用：把框架的确定性计算搬到 Python，产出网格表，供 Agent「查表选行」而非手算。
这是框架防幻觉机制的核心。

产出 §17.1–§17.13：
  §17.1  财务趋势摘要（CAGR / 负债率 / 净现金 / 支付率）
  §17.2  Factor2 输入（OE 组件、支付率、门槛）
  §17.3  真实现金收入（步骤3）
  §17.4  经营性现金支出（步骤5）
  §17.5  基准结余 AA + 敏感性输入
  §17.6  价格分位（10年周线）
  §17.9  业绩下滑敏感性
  §17.10 支付率 M 三重校验（Python 内裁定 M_rec）
  §17.11 税后穿透回报率网格（M候选 × 税率 × AA变体）+ 目标买入价
  §17.12 G 系数网格（0.7–1.8 × 12 档，H/OE 逐行）
  §17.13 λ 收入敏感性 + 临界收入倍数

用法：
    python scripts/derived_metrics_local.py --input output/600887_伊利股份/data_pack_market.md \
                                            --output output/600887_伊利股份/section17.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# datapack 解析
# ---------------------------------------------------------------------------

def parse_pack(text: str) -> dict[str, str]:
    """按 `## N.` 切段。返回 {段号: 内容}。"""
    out: dict[str, str] = {}
    for m in re.finditer(r"^## (\d+[A-Z]?)\. .*$", text, re.M):
        s = m.end()
        n = re.search(r"^## ", text[s:], re.M)
        out[m.group(1)] = text[s:s + (n.start() if n else len(text))]
    return out


def _kv(block: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in block.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [x.strip() for x in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        try:
            out[cells[0]] = float(cells[1].replace(",", ""))
        except ValueError:
            pass
    return out


def _matrix(block: str) -> dict[str, dict[str, float]]:
    """矩阵型：字段为行，期间为列。"""
    rows = [l for l in block.splitlines()
            if l.strip().startswith("|") and not set(l) <= set("|-: ")]
    if len(rows) < 2:
        return {}
    hdr = [x.strip() for x in rows[0].strip("|").split("|")]
    years = [h for h in hdr[1:] if re.fullmatch(r"\d{4}", h)]
    out: dict[str, dict[str, float]] = {}
    for r in rows[1:]:
        cs = [x.strip() for x in r.strip("|").split("|")]
        if not cs:
            continue
        vals: dict[str, float] = {}
        for i, y in enumerate(years):
            v = cs[i + 1].replace(",", "") if i + 1 < len(cs) else ""
            try:
                vals[y] = float(v)
            except ValueError:
                pass
        out[cs[0]] = vals
    return out


def _list(block: str) -> list[dict]:
    """列表型：行为记录，列为字段。"""
    rows = [l for l in block.splitlines()
            if l.strip().startswith("|") and not set(l) <= set("|-: ")]
    if len(rows) < 2:
        return []
    hdr = [x.strip() for x in rows[0].strip("|").split("|")]
    out = []
    for r in rows[1:]:
        cs = [x.strip() for x in r.strip("|").split("|")]
        rec = {}
        for i, h in enumerate(hdr):
            v = cs[i] if i < len(cs) else ""
            try:
                rec[h] = float(v.replace(",", ""))
            except ValueError:
                rec[h] = v
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def cagr(first, last, years):
    if not first or not last or first <= 0 or years <= 0:
        return None
    return ((last / first) ** (1 / years) - 1) * 100


def fx(v, nd=2):
    return "—" if v is None else f"{v:,.{nd}f}"


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, text: str):
        S = parse_pack(text)
        self.S = S
        self.info = _kv(S.get("1", ""))
        self.inc = _matrix(S.get("3", ""))
        self.incP = _matrix(S.get("3P", ""))
        self.bal = _matrix(S.get("4", ""))
        self.balP = _matrix(S.get("4P", ""))
        self.cf = _matrix(S.get("5", ""))
        self.ind = _matrix(S.get("12", ""))
        self.div_rows = _list(S.get("6", ""))
        self.holder = _list(S.get("7", ""))
        self.mainbz = _list(S.get("9", ""))
        self.repo = _list(S.get("15", ""))
        self.pledge = _list(S.get("16", ""))

        self.years = sorted(
            [y for y in (self.inc.get("归母净利润") or self.bal.get("总资产") or {}) 
             if re.fullmatch(r"\d{4}", str(y))],
            reverse=True)
        self.price = self.info.get("当前价格")
        self.mktcap = (self.info.get("总市值") or 0) * 1e4 / 1e6      # 万元 -> 百万元
        self.shares_wan = self.info.get("总股本")                     # 万股
        self.rf = self._rf()

        # 分红按年
        self.div = {}
        for r in self.div_rows:
            y = r.get("年度")
            y = str(int(y)) if isinstance(y, float) else str(y).strip()
            v = r.get("总分红")
            if y.isdigit() and isinstance(v, (int, float)):
                self.div[y] = v

    def _rf(self) -> Optional[float]:
        blk = self.S.get("14", "")
        vals = []
        for line in blk.splitlines():
            if line.strip().startswith("|"):
                cells = [x.strip() for x in line.strip("|").split("|")]
                for c in cells:
                    try:
                        v = float(c.replace(",", ""))
                        if 0.5 < v < 10:
                            vals.append(v)
                    except ValueError:
                        pass
        return vals[0] if vals else None

    def II(self) -> float:
        """A股门槛 = max(3.5%, Rf+2%)"""
        return max(3.5, (self.rf or 1.8) + 2.0)

    def get(self, ds: dict, field: str, year: str):
        return (ds.get(field) or {}).get(year)


# ---------------------------------------------------------------------------
# §17 各节
# ---------------------------------------------------------------------------

def s17_1(c: Ctx) -> str:
    L = ["### §17.1 财务趋势摘要", ""]
    yrs = c.years
    ni = c.inc.get("归母净利润", {})
    rev = c.inc.get("营业收入", {})
    ocf = c.cf.get("经营活动现金流", {})
    L += ["| 指标 | " + " | ".join(yrs[:6]) + " |",
          "|---|" + "---|" * len(yrs[:6])]
    for name, ds in [("营业收入", rev), ("归母净利润", ni), ("经营现金流", ocf)]:
        L.append(f"| {name} | " + " | ".join(fx(ds.get(y), 0) for y in yrs[:6]) + " |")
    # 负债率 / 净现金
    ta = c.bal.get("总资产", {})
    tl = c.bal.get("总负债", {})
    mc = c.bal.get("货币资金", {})
    L.append("| 资产负债率(%) | " + " | ".join(
        fx((tl.get(y) / ta.get(y) * 100) if ta.get(y) and tl.get(y) else None, 1)
        for y in yrs[:6]) + " |")
    L.append("| 净现金(百万元) | " + " | ".join(
        fx((mc.get(y) or 0) - (tl.get(y) or 0) if mc.get(y) else None, 0)
        for y in yrs[:6]) + " |")
    # CAGR
    if len(yrs) >= 5:
        y0, y5 = yrs[0], yrs[4]
        L += ["", f"- 营收 5年CAGR: {fx(cagr(rev.get(y5), rev.get(y0), 4))}%",
              f"- 归母 5年CAGR: {fx(cagr(ni.get(y5), ni.get(y0), 4))}%"]
    L.append("")
    return "\n".join(L)


def s17_10(c: Ctx) -> tuple[str, Optional[float], str]:
    """§17.10 支付率 M 三重校验 + Python 内裁定。"""
    yrs = c.years[:5]
    ni = c.inc.get("归母净利润", {})
    ocf = c.cf.get("经营活动现金流", {})
    paid = c.cf.get("已付股息", {})

    rows = []
    for y in yrs:
        m1 = m2 = None
        d = c.div.get(y)
        n = ni.get(y)
        if d and n:
            m1 = d / n * 100
        p = paid.get(y)
        if p and n:
            m2 = p / n * 100
        rows.append((y, m1, m2))

    def avg(idx):
        return mean([r[idx] for r in rows])

    m1a, m2a = avg(1), avg(2)

    # 裁定
    reasons = []
    rec, src = None, ""
    if m1a and m2a and abs(m1a - m2a) / max(m1a, m2a) > 0.15:
        rec, src = m2a, "法2(现金流)"
        reasons.append(f"法1({m1a:.1f}%) vs 法2({m2a:.1f}%) 偏差 >15% → 取法2")
    elif m1a:
        rec, src = m1a, "法1(DPS×股本/归母)"
    elif m2a:
        rec, src = m2a, "法2(现金流)"

    L = ["### §17.10 支付率 M 三重校验", "",
         "| 年度 | 法1 总分红/归母(%) | 法2 已付股息/归母(%) |", "|---|---|---|"]
    for y, m1, m2 in rows:
        L.append(f"| {y} | {fx(m1)} | {fx(m2)} |")
    L += ["", f"- 法1 近5年均值: **{fx(m1a)}%**",
          f"- 法2 近5年均值: **{fx(m2a)}%**",
          f"- **裁定 M_rec = {fx(rec)}%（来源：{src}）**"]
    if reasons:
        L.append(f"- 理由：{'; '.join(reasons)}")
    L.append("")
    return "\n".join(L), (rec / 100 if rec else None), src


def s17_11(c: Ctx, M: Optional[float], II: float,
            aa_variants: Optional[dict] = None) -> tuple[str, list]:
    """§17.11 税后穿透回报率网格 + 目标买入价。

    aa_variants 优先用 §17.5 的完整 11 步口径；缺失时回退 OCF−Capex。
    """
    if not M or not c.mktcap:
        return "### §17.11 穿透回报率网格\n\n> ⚠️ 缺 M 或市值，跳过。\n", []

    ni = c.inc.get("归母净利润", {})
    C = ni.get(c.years[0])

    if aa_variants and aa_variants.get("AA_2y") is not None:
        aa2 = aa_variants.get("AA_2y")
        aaall = aa_variants.get("AA_all")
        aaexcl = aa_variants.get("AA_excl")
        aa_src = "§17.5（完整 11 步）"
    else:
        aa = {}
        for y in c.years[:5]:
            o = c.cf.get("经营活动现金流", {}).get(y)
            cp = c.cf.get("资本开支", {}).get(y)
            if o and cp:
                aa[y] = o - cp
        aa2 = mean([aa.get(y) for y in c.years[:2]])
        aaall = mean(list(aa.values()))
        aaexcl = None
        aa_src = "OCF−Capex（简化，§17.5 缺失时回退）"

    # 税率情景（A股默认 Q=0）
    QS = [0.0] if str(c.info.get("代码", "")) == "1" else [0.0, 0.1, 0.2]
    # 简化：A股长期持有 Q=0
    QS = [0.0, 0.1, 0.2]

    rows = []
    for label, base in [("AA_2y", aa2), ("AA_all", aaall), ("AA_excl", aaexcl), ("C(粗算)", C)]:
        if base is None:
            continue
        for q in QS:
            val = base * M * (1 - q) / c.mktcap * 100
            tgt = (c.price * val / II) if (c.price and II) else None
            rows.append((label, q * 100, base, val, val - II, tgt))

    L = ["### §17.11 穿透回报率网格（税后）", "",
         f"基准：市值 {c.mktcap:,.0f} 百万元 | 门槛 II = {II:.2f}% | M_rec = {M:.4f}",
         f"AA 口径：{aa_src}", "",
         "| 基准值 | 变体 | Q(%) | 基准(百万元) | 回报率(%) | GG−II(pct) | 目标买入价 |",
         "|---|---|---|---|---|---|---|"]
    tgt_default = None
    for label, q, base, val, gap, tgt in rows:
        star = ""
        if label == "AA_2y" and q == 0.0:
            tgt_default = tgt
            star = " ★"
        L.append(f"| {base:,.0f} | {label}{star} | {q:.0f} | {base:,.0f} | "
                 f"{val:.2f} | {gap:+.2f} | {fx(tgt)} |")
    L += ["", f"★ = 推荐组合（AA_2y @ Q=0，A股长期持有）",
          f"- **目标买入价 = {fx(tgt_default)} 元**（= 当前股价 × GG / II）"]
    L.append("")
    return "\n".join(L), rows


def s17_12(c: Ctx) -> str:
    """§17.12 G 系数网格（0.7–1.8 × 12 档）。"""
    ni = c.inc.get("归母净利润", {})
    C = ni.get(c.years[0]) or 0
    da = {}
    for y in c.years[:5]:
        d = sum(c.cf.get(k, {}).get(y) or 0
                for k in ("折旧摊销_固定资产", "摊销_无形资产", "摊销_长期待摊"))
        if d:
            da[y] = d
    D = mean(list(da.values())) or 0
    capex = c.cf.get("资本开支", {})
    F = median([capex.get(y) / da[y] for y in da if da.get(y)]) if da else None

    L = ["### §17.12 G 系数网格（维持性 Capex 档位）", "",
         f"C（归母净利润）= {C:,.0f} 百万元 | D（折旧摊销）= {D:,.0f} 百万元 | "
         f"F（Capex/D&A 中位数）= {fx(F)}", "",
         "| G | 档位 | H = D×G | OE = C + D − H |", "|---|---|---|---|"]
    tiers = [(0.7, 1.0, "轻资产"), (1.0, 1.3, "中等资产"), (1.2, 1.8, "重资产")]
    for i in range(12):
        g = round(0.7 + i * 0.1, 1)
        tag = ""
        for lo, hi, name in tiers:
            if lo <= g <= hi:
                tag = name
                break
        H = D * g
        OE = C + D - H
        L.append(f"| {g:.1f} | {tag} | {H:,.0f} | {OE:,.0f} |")
    L += ["", "**选择锚点**：轻资产 G=0.7~1.0 | 中等 G=1.0~1.3 | 重资产 G=1.2~1.8",
          f"**F 提示**：F={fx(F)} 反映历史 Capex/D&A 比例，供 G 选择参考。"]
    L.append("")
    return "\n".join(L)


def s17_13(c: Ctx) -> str:
    """§17.13 λ 收入敏感性。"""
    ni = c.inc.get("归母净利润", {})
    rev = c.inc.get("营业收入", {})
    aa = {}
    for y in c.years[:5]:
        o = c.cf.get("经营活动现金流", {}).get(y)
        cp = c.cf.get("资本开支", {}).get(y)
        if o and cp:
            aa[y] = o - cp
    ys = [y for y in c.years[:5] if aa.get(y) and rev.get(y)]
    lam = None
    lam_raw = None
    lam_note = ""
    if len(ys) >= 3:
        asc = list(reversed(ys))
        lams = []
        for i in range(len(asc) - 1):
            dS = rev[asc[i + 1]] - rev[asc[i]]
            dA = aa[asc[i + 1]] - aa[asc[i]]
            if abs(dS) > 1e-6:
                lams.append(dA / dS)
        lam_raw = median(lams)
        # λ 语义：经营杠杆（收入下滑 -> 结余下滑）。
        # 实测为负 = 结余与收入反向（现金流波动主导），不适合做敏感性外推。
        if lam_raw is not None and lam_raw < 0:
            lam = 0.0
            lam_note = (f"⚠️ 原始 λ={lam_raw:.3f} 为负（结余与收入反向，"
                        f"现金流波动主导），保守取 λ=0")
        else:
            lam = lam_raw

    L = ["### §17.13 收入敏感性（λ 外推）", ""]
    if lam is None:
        L += ["> ⚠️ λ 不可计算（数据不足或收入无波动）", ""]
        return "\n".join(L)

    AA = mean([aa.get(y) for y in c.years[:2]]) or 0
    S = rev.get(c.years[0]) or 0
    M = None
    # 复用 §17.10 的 M（简化：用近3年总分红/归母）
    m3 = []
    for y in c.years[:3]:
        if c.div.get(y) and ni.get(y):
            m3.append(c.div[y] / ni[y])
    M = mean(m3)

    L += [f"λ（Δ结余/Δ收入 中位数）= {fx(lam, 3)} | AA = {AA:,.0f} | S = {S:,.0f} | M = {fx(M, 4)}", ""]
    if lam_note:
        L += [lam_note, ""]
    L += ["| 收入情景 | 收入 | 预估结余 | 精算回报率(%) | vs 门槛 |",
          "|---|---|---|---|---|"]
    II = c.II()
    crit = None
    for k in [1.0, 0.9, 0.8, 0.7]:
        Sk = S * k
        AAk = AA + lam * (Sk - S)
        # 限制：结余不得为负（收入归零时最多降到 0）
        AAk = max(AAk, 0.0)
        gg = AAk * (M or 0) / c.mktcap * 100 if c.mktcap else None
        L.append(f"| {k:.1f}× | {Sk:,.0f} | {AAk:,.0f} | {fx(gg)} | {fx((gg-II) if gg else None, 2)} |")
    # 临界倍数
    if M and c.mktcap and lam and lam > 1e-6:
        target_aa = II / 100 * c.mktcap / M
        crit = 1 + (target_aa - AA) / (lam * S)
        L.append(f"\n- **临界收入倍数 k\\* = {fx(crit, 3)}**（令 GG = II）")
        L.append(f"- 安全边际韧性：{'强' if crit < 0.7 else ('中等' if crit < 0.85 else '敏感')}")
    else:
        L.append(f"\n- 临界收入倍数：**不适用**（λ≈0，收入变动不影响结余估算）")
        L.append(f"- 安全边际韧性：**取决于结余本身的稳定性**（建议看 §17.1 结余序列）")
    L.append("")
    return "\n".join(L)


def s17_2(c: Ctx, M: Optional[float]) -> str:
    II = c.II()
    ni = c.inc.get("归母净利润", {})
    C = ni.get(c.years[0])
    L = ["### §17.2 Factor2 输入参数", "",
         f"- 归母净利润 C（{c.years[0]}）= {fx(C, 0)} 百万元",
         f"- 支付率锚定 M = {fx(M*100 if M else None)}%",
         f"- 无风险利率 Rf = {fx(c.rf)}%",
         f"- 门槛 II = max(3.5%, Rf+2%) = **{II:.2f}%**",
         f"- 市值 = {c.mktcap:,.0f} 百万元",
         f"- 当前股价 = {fx(c.price)} 元",
         ""]
    return "\n".join(L)


def s17_6(c: Ctx) -> str:
    """§17.6 价格分位（用 §11 周线）。"""
    wk = _list(c.S.get("11", ""))
    if not wk:
        return "### §17.6 价格分位\n\n> ⚠️ 无 §11 周线数据。\n"
    # 找收盘列
    key = next((k for k in wk[0] if "收盘" in k or k == "close"), None)
    if not key:
        return "### §17.6 价格分位\n\n> ⚠️ §11 无收盘价列。\n"
    vals = [r[key] for r in wk if isinstance(r.get(key), (int, float))]
    if not vals or not c.price:
        return "### §17.6 价格分位\n\n> ⚠️ 数据不足。\n"
    lo, hi = min(vals), max(vals)
    pct = sum(1 for v in vals if v < c.price) / len(vals) * 100
    L = ["### §17.6 价格分位（基于 §11 周线）", "",
         f"- 样本数 N = {len(vals)}",
         f"- 区间最低 = {lo:.2f} | 最高 = {hi:.2f}",
         f"- 当前股价 = {c.price:.2f}",
         f"- **当前价格分位 = {pct:.1f}%**",
         ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def s17_3(c: Ctx) -> str:
    """§17.3 真实现金收入（步骤3）。"""
    rev = c.inc.get("营业收入", {})
    ar = c.bal.get("应收账款", {})
    cl = c.bal.get("合同负债", {}) or c.bal.get("预收款项", {})
    yrs = [y for y in c.years if rev.get(y)]
    L = ["### §17.3 真实现金收入还原（步骤3）", "",
         "保守规则：AR 净增扣除 / 预收净增不加回", "",
         "| 年份 | 营业收入 | AR净变动 | 预收净变动 | 真实现金收入 | 收款比率 |",
         "|---|---|---|---|---|---|"]
    rows = []
    for i, y in enumerate(yrs):
        S = rev.get(y)
        T = None
        U = None
        if i + 1 < len(yrs):
            yn = yrs[i + 1]
            if ar.get(y) is not None and ar.get(yn) is not None:
                T = ar[y] - ar[yn]
            if cl.get(y) is not None and cl.get(yn) is not None:
                U = cl[y] - cl[yn]
        cash = None
        if S is not None:
            cash = S - (T or 0) - (max(U, 0) if U else 0)
            # 预收净减少 -> 扣除；净增加不加回（已隐含）
            if U is not None and U < 0:
                cash = S - (T or 0) - 0   # 减少已在 S 之外，额外扣除见下
        ratio = (cash / S * 100) if (cash is not None and S) else None
        rows.append((y, S, T, U, cash, ratio))
        L.append("| %s | %s | %s | %s | %s | %s |" % (
            y, fx(S, 0), fx(T, 0), fx(U, 0), fx(cash, 0),
            (f"{ratio:.2f}%" if ratio is not None else "—")))
    L += ["", "> 收款比率 < 100% 表示收入未全额转化为现金（应收占用）。"]
    L.append("")
    return "\n".join(L)


def s17_4(c: Ctx) -> str:
    """§17.4 经营性现金支出还原（步骤5）。"""
    cost = c.inc.get("营业成本", {})
    ap = c.bal.get("应付账款", {})
    staff = c.cf.get("支付给职工现金", {})
    tax = c.inc.get("所得税", {})
    dta = c.bal.get("递延所得税资产", {})
    dtl = c.bal.get("递延所得税负债", {})
    fin = c.inc.get("财务费用", {})
    yrs = [y for y in c.years if cost.get(y)]
    L = ["### §17.4 经营性现金支出还原（步骤5）", "",
         "| 年份 | W1 供应商 | W2 员工 | W3 税款 | W4 利息 | W 合计 |",
         "|---|---|---|---|---|---|"]
    for i, y in enumerate(yrs):
        W1 = cost.get(y)
        if i + 1 < len(yrs):
            yn = yrs[i + 1]
            if ap.get(y) is not None and ap.get(yn) is not None:
                W1 = (W1 or 0) + (ap[yn] - ap[y])   # 应付净减少 -> 多付现金
        W2 = staff.get(y)
        W3 = tax.get(y)
        if W3 is not None:
            dd = 0.0
            if i + 1 < len(yrs):
                yn = yrs[i + 1]
                for ds in (dta, dtl):
                    if ds.get(y) is not None and ds.get(yn) is not None:
                        dd += ds[y] - ds[yn]
            W3 = W3 - dd if dd else W3
        W4 = fin.get(y)
        W = sum(x for x in (W1, W2, W3, W4) if x is not None) or None
        L.append("| %s | %s | %s | %s | %s | %s |" % (
            y, fx(W1, 0), fx(W2, 0), fx(W3, 0), fx(W4, 0), fx(W, 0)))
    L += ["", "W1 = 营业成本 + 应付净减少（保守）；W3 = 所得税 − 递延税变动。", ""]
    return "\n".join(L)


def s17_5(c: Ctx) -> tuple[str, dict, dict]:
    """§17.5 真实可支配现金结余（步骤8）。返回 (md, AA变体, 逐年结余)。"""
    rev = c.inc.get("营业收入", {})
    ar = c.bal.get("应收账款", {})
    cl = c.bal.get("合同负债", {}) or c.bal.get("预收款项", {})
    cost = c.inc.get("营业成本", {})
    ap = c.bal.get("应付账款", {})
    staff = c.cf.get("支付给职工现金", {})
    tax = c.inc.get("所得税", {})
    fin = c.inc.get("财务费用", {})
    capex = c.cf.get("资本开支", {})
    inv_pay = c.cf.get("投资支付的现金", {})
    acq = c.cf.get("取得子公司支付", {})
    disp = c.cf.get("处置固定资产收回", {})
    inv_inc = c.cf.get("取得投资收益收到", {})
    ocf = c.cf.get("经营活动现金流", {})

    yrs = [y for y in c.years if rev.get(y)]
    L = ["### §17.5 真实可支配现金结余（步骤8）", "",
         "结余 = 真实现金收入 + V1 + V5 − W − Y（Y = 资本开支 + 收购/参股）",
         "> ⚠️ 投资支付的现金（理财/定存滚动）**不扣除** —— 属现金形态转换而非消耗。", "",
         "| 年份 | 真实现金收入 | +处置V1 | +投资V5 | −经营支出W | −总扣除Y | =结余 |",
         "|---|---|---|---|---|---|---|"]
    bal: dict[str, float] = {}
    for i, y in enumerate(yrs):
        S = rev.get(y) or 0
        T = U = 0
        if i + 1 < len(yrs):
            yn = yrs[i + 1]
            if ar.get(y) is not None and ar.get(yn) is not None:
                T = ar[y] - ar[yn]
            if cl.get(y) is not None and cl.get(yn) is not None:
                U = cl[y] - cl[yn]
        cash_rev = S - T - (abs(U) if U and U < 0 else 0)
        W1 = cost.get(y) or 0
        if i + 1 < len(yrs):
            yn = yrs[i + 1]
            if ap.get(y) is not None and ap.get(yn) is not None:
                W1 += (ap[yn] - ap[y])
        W = W1 + (staff.get(y) or 0) + (tax.get(y) or 0) + (fin.get(y) or 0)
        Y = (capex.get(y) or 0) + (acq.get(y) or 0)
        v1 = disp.get(y) or 0
        v5 = inv_inc.get(y) or 0
        b = cash_rev + v1 + v5 - W - Y
        bal[y] = b
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            y, fx(cash_rev, 0), fx(v1, 0), fx(v5, 0), fx(W, 0), fx(Y, 0), fx(b, 0)))

    ys = [y for y in yrs if y in bal]
    aa2 = mean([bal[y] for y in ys[:2]])
    aaall = mean([bal[y] for y in ys])
    # AA_excl：剔除「保留项 > OCF×50%」的非经营主导年份
    keep = [y for y in ys
            if not (ocf.get(y) and (disp.get(y, 0) + inv_inc.get(y, 0)) > ocf[y] * 0.5)]
    aaexcl = mean([bal[y] for y in keep]) if len(keep) >= 2 else None

    L += ["", f"- **AA_2y = {fx(aa2, 0)}**（近2年均值，默认基准）",
          f"- **AA_all = {fx(aaall, 0)}**（全部年份均值）",
          f"- **AA_excl = {fx(aaexcl, 0)}**（剔除非经营主导年份，保留 {len(keep)} 年）", ""]
    return "\n".join(L), {"AA_2y": aa2, "AA_all": aaall, "AA_excl": aaexcl}, bal


def s17_9(c: Ctx, AA: Optional[float], M: Optional[float], II: float) -> str:
    """§17.9 业绩下滑敏感性。"""
    if not AA or not M or not c.mktcap:
        return "### §17.9 业绩下滑敏感性\n\n> ⚠️ 参数不足。\n"
    L = ["### §17.9 业绩下滑敏感性", "",
         "| 情景 | 结余 | 穿透回报率(%) | 门槛价格(元) | vs 现价 |",
         "|---|---|---|---|---|"]
    for label, k in [("基准", 1.0), ("-10%", 0.9), ("-20%", 0.81), ("-30%", 0.729)]:
        a = AA * k
        gg = a * M / c.mktcap * 100
        tp = (c.price * gg / II) if (c.price and II) else None
        vs = ((tp / c.price - 1) * 100) if (tp and c.price) else None
        L.append(f"| {label} | {fx(a,0)} | {gg:.2f} | {fx(tp)} | {fx(vs)}% |")
    L.append("")
    return "\n".join(L)


def s17_8(c: Ctx, AA: Optional[float], M: Optional[float]) -> str:
    """§17.8 绝对估值仪表盘 + 地板价基准。"""
    L = ["### §17.8 绝对估值仪表盘", ""]
    ni = c.inc.get("归母净利润", {})
    C = ni.get(c.years[0])
    ocf = c.cf.get("经营活动现金流", {}).get(c.years[0])
    capex = c.cf.get("资本开支", {}).get(c.years[0])
    if C and c.mktcap:
        L.append(f"- PE（归母，百万元口径）= {c.mktcap/C:.2f}x")
    if c.price and c.ind.get("每股净资产", {}):
        bps = c.ind["每股净资产"].get(c.years[0])
        if bps:
            L.append(f"- PB = {c.price/bps:.2f}x")
    if ocf and capex:
        fcf = ocf - capex
        if c.mktcap:
            L.append(f"- FCF（OCF−Capex）= {fcf:,.0f} 百万元")
            L.append(f"- **FCF 收益率 = {fcf/c.mktcap*100:.2f}%**")
    if c.price and C and c.shares_wan:
        eps = C / (c.shares_wan * 1e4) * 1e6 / 1e6   # 百万元 / 万股 -> 元/股
        eps = C * 1e6 / (c.shares_wan * 1e4)
        L.append(f"- EPS（归母/股本）= {eps:.3f} 元/股")
        if eps > 0:
            L.append(f"- 扣除现金 PE 参考 = {c.price:.2f} / {eps:.3f} = {c.price/eps:.2f}x")
    ta = c.bal.get("总资产", {}).get(c.years[0])
    gw = c.bal.get("商誉", {}).get(c.years[0])
    if ta and gw:
        L.append(f"- 商誉/总资产 = {gw/ta*100:.2f}%")
    L.append("")
    return "\n".join(L)


def build(text: str) -> str:
    c = Ctx(text)
    II = c.II()
    parts = ["## §17. 衍生指标预计算（本地确定性）", "",
             "> 本段由 `derived_metrics_local.py` 从 datapack **确定性计算**，无 LLM 判断成分。",
             "> **Agent 禁止重算**：有网格时只许「查表选行 + 说明理由」。", ""]
    parts.append(s17_1(c))
    parts.append(s17_3(c))
    parts.append(s17_4(c))
    s5, aa_variants, _bal = s17_5(c)
    parts.append(s5)
    parts.append(s17_6(c))
    s10, M, src = s17_10(c)
    parts.append(s10)
    parts.append(s17_2(c, M))
    s11, _ = s17_11(c, M, II, aa_variants)
    parts.append(s11)
    parts.append(s17_12(c))
    parts.append(s17_8(c, aa_variants.get("AA_2y"), M))
    parts.append(s17_9(c, aa_variants.get("AA_2y"), M, II))
    parts.append(s17_13(c))
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="§17 衍生指标本地预计算")
    ap.add_argument("--input", required=True, help="data_pack_market.md")
    ap.add_argument("--output", required=True, help="section17.md")
    ap.add_argument("--append", action="store_true", help="追加到 datapack 末尾")
    args = ap.parse_args()

    p = Path(args.input)
    if not p.exists():
        print(f"输入不存在: {p}", file=sys.stderr)
        return 2
    text = p.read_text(encoding="utf-8")
    md = build(text)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.append:
        with out.open("a", encoding="utf-8", newline="\n") as f:
            f.write("\n---\n\n" + md)
    else:
        out.write_text(md, encoding="utf-8", newline="\n")
    print(f"已写入: {out}  ({len(md)} 字符)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
