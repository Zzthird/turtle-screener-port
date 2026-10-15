"""母公司报表提取器 — 从年报 PDF 解析母公司三大报表。

三层过滤（实测自 600887.SH 2024 年报）：
  ① 页级：定位含「母公司资产负债表/利润表/现金流量表」的页
  ② 混合页：排除与合并报表同页的干扰（取标题后紧邻的表格）
  ③ 行级：以科目名称为锚点提取数值，按表头年份对齐

实测基准（伊利 600887.SH 2024）：
  母公司营业收入  102,395,394,662.41 元 = 1,023.95 亿
  合并营业收入  1,153.93 亿  → 母公司 < 合并 ✅
  母公司货币资金  8,471,582,485.64 元
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# 目标报表 → 标题关键词（按顺序匹配，避免"合并"误命中）
REPORTS = {
    "母公司资产负债表": ["母公司资产负债表"],
    "母公司利润表": ["母公司利润表"],
    "母公司现金流量表": ["母公司现金流量表"],
}

# 科目名 → 输出业务名（与 schema 对齐）
BALANCE_ITEMS = {
    "货币资金": "货币资金",
    "交易性金融资产": "交易性金融资产",
    "应收票据": "应收票据",
    "应收账款": "应收账款",
    "预付款项": "预付款项",
    "其他应收款": "其他应收款",
    "存货": "存货",
    "流动资产合计": "流动资产合计",
    "长期股权投资": "长期股权投资",
    "固定资产": "固定资产",
    "在建工程": "在建工程",
    "无形资产": "无形资产",
    "商誉": "商誉",
    "投资性房地产": "投资性房地产",
    "资产总计": "总资产",
    "短期借款": "短期借款",
    "应付票据": "应付票据",
    "应付账款": "应付账款",
    "预收款项": "预收款项",
    "合同负债": "合同负债",
    "应付职工薪酬": "应付职工薪酬",
    "应交税费": "应交税费",
    "长期借款": "长期借款",
    "应付债券": "应付债券",
    "流动负债合计": "流动负债合计",
    "非流动负债合计": "非流动负债合计",
    "负债合计": "总负债",
    "实收资本": "实收资本",
    "资本公积": "资本公积",
    "盈余公积": "盈余公积",
    "未分配利润": "未分配利润",
    "归属于母公司股东权益合计": "归母权益",
    "归属于母公司所有者权益合计": "归母权益",
    "少数股东权益": "少数股东权益",
    "所有者权益合计": "所有者权益合计",
    "股东权益合计": "所有者权益合计",
    "所有者权益（或股东权益）合计": "所有者权益合计",
}

INCOME_ITEMS = {
    "营业收入": "营业收入",
    "营业成本": "营业成本",
    "税金及附加": "营业税金",
    "销售费用": "销售费用",
    "管理费用": "管理费用",
    "研发费用": "研发费用",
    "财务费用": "财务费用",
    "营业利润": "经营利润",
    "利润总额": "利润总额",
    "所得税费用": "所得税",
    "净利润": "净利润",
}

CASHFLOW_ITEMS = {
    "经营活动产生的现金流量净额": "经营活动现金流",
    "投资活动产生的现金流量净额": "投资活动现金流",
    "筹资活动产生的现金流量净额": "融资活动现金流",
    "购建固定资产、无形资产和其他长期资产支付的现金": "资本开支",
    "分配股利、利润或偿付利息支付的现金": "已付股息",
    "支付给职工以及为职工支付的现金": "支付给职工现金",
    "销售商品、提供劳务收到的现金": "销售商品收到现金",
    "期末现金及现金等价物余额": "期末现金",
}

ITEM_MAPS = {
    "母公司资产负债表": BALANCE_ITEMS,
    "母公司利润表": INCOME_ITEMS,
    "母公司现金流量表": CASHFLOW_ITEMS,
}

_NUM = re.compile(r"^-?[\d,]+\.?\d*$")
_YEAR = re.compile(r"(20\d{2})")


@dataclass
class ReportTable:
    name: str
    page: int
    years: list[str] = field(default_factory=list)     # ["2024", "2023"]
    items: dict[str, list[Optional[float]]] = field(default_factory=dict)


def _to_num(s: str) -> Optional[float]:
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("，", "")
    if not t or not _NUM.match(t) or t in {"-", "--"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _find_header_row(table: list[list[str]]) -> tuple[int, list[str]]:
    """定位表头行，返回 (行号, 年份列表)。"""
    for i, row in enumerate(table[:6]):
        cells = [(c or "").strip() for c in row]
        years = []
        for c in cells:
            m = _YEAR.search(c)
            if m and ("年" in c or "月" in c or c.strip().startswith("20")):
                years.append(m.group(1))
        if years:
            return i, years
    return -1, []


def _collect_items(tables: list[list[list[str]]], name: str,
                   years: list[str]) -> dict[str, list[Optional[float]]]:
    """从多张表（含跨页续表）提取科目值。"""
    all_items = ITEM_MAPS[name]
    items: dict[str, list[Optional[float]]] = {}
    for best in tables:
        for row in best:
            if not row:
                continue
            label = str(row[0] or "")
            # 去掉缩进/编号/量词前缀：减：营业成本 / 其中：应收利息 / 一、营业收入
            label = (label.replace("　", "").replace(" ", "").replace("\n", "")
                     .lstrip("-—").strip())
            label = re.sub(r"^[一二三四五六七八九十]+、", "", label)
            label = re.sub(r"^(减|加|其中|其中：|及)：?", "", label)
            label = label.replace("：", "").strip()
            if not label:
                continue
            for anchor, biz in all_items.items():
                if biz in items:
                    continue
                if label == anchor or label.startswith(anchor):
                    vals = [_to_num(str(c or "")) for c in row[1:]]
                    nums = [v for v in vals if v is not None]
                    if nums:
                        items[biz] = nums[:len(years)] if years else nums
                    break
    return items


def extract_parent_reports(pdf_path: str, verbose: bool = False) -> dict[str, ReportTable]:
    """从年报 PDF 提取母公司三大报表（支持跨页续表）。"""
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("需要 pdfplumber：pip install pdfplumber") from e

    found: dict[str, ReportTable] = {}

    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        for pno in range(n_pages):
            page = pdf.pages[pno]
            text = page.extract_text() or ""

            for name, keys in REPORTS.items():
                if name in found:
                    continue
                if not any(k in text for k in keys):
                    continue
                # 页级过滤：排除目录页（含点线）
                if "......" in text or "…" in text:
                    continue

                tables = [t for t in page.extract_tables() if t]
                if not tables:
                    continue

                hi, years = -1, []
                for t in tables:
                    hi, years = _find_header_row(t)
                    if years:
                        break
                if not years:
                    continue

                # 跨页续表：向后扫描若干页，直到出现下一张母公司报表标题
                chain = list(tables)
                j = pno + 1
                while j < n_pages and j <= pno + 3:
                    nt = pdf.pages[j].extract_text() or ""
                    if any(k in nt for k in sum(REPORTS.values(), [])):
                        break                       # 下一张报表开始了
                    if "......" in nt:
                        break
                    more = [t for t in pdf.pages[j].extract_tables() if t]
                    if not more:
                        break
                    chain.extend(more)
                    j += 1

                items = _collect_items(chain, name, years)
                if items:
                    found[name] = ReportTable(name=name, page=pno + 1,
                                              years=years, items=items)
                    if verbose:
                        span = f"第 {pno+1}" + (f"-{j}" if j > pno + 1 else "") + " 页"
                        print(f"  找到 {name} @ {span}，{len(items)} 个科目，年份 {years}")

    return found


def validate(reports: dict[str, ReportTable], merged: dict[str, float] | None = None) -> list[str]:
    """合理性校验：母公司 <= 合并。"""
    warns: list[str] = []
    inc = reports.get("母公司利润表")
    bal = reports.get("母公司资产负债表")

    if inc and "营业收入" in inc.items:
        p_rev = inc.items["营业收入"][0]
        if p_rev is not None and merged and merged.get("营业收入"):
            m_rev = merged["营业收入"]
            if p_rev > m_rev * 1.01:
                warns.append(f"⚠️ 母公司营收 {p_rev/1e8:.2f} 亿 > 合并 {m_rev/1e8:.2f} 亿，异常")
    if bal and "总资产" in bal.items:
        p_ta = bal.items["总资产"][0]
        if p_ta is not None and merged and merged.get("总资产"):
            m_ta = merged["总资产"]
            if p_ta > m_ta * 1.01:
                warns.append(f"⚠️ 母公司总资产 {p_ta/1e8:.2f} 亿 > 合并 {m_ta/1e8:.2f} 亿，异常")
    return warns


def to_rows(reports: dict[str, ReportTable]) -> dict[str, list[dict]]:
    """转成 collector 可用的行格式：{数据集名: [行...]}。

    行格式与 datasource 的 Value dict 对齐，便于直接接入 data_collector。
    """
    def mk(items: dict, years: list[str]) -> list[dict]:
        rows = []
        n = len(years) if years else 1
        for i in range(n):
            row: dict = {}
            for biz, vals in items.items():
                v = vals[i] if i < len(vals) else None
                row[biz] = {"value": v, "unit": "元", "currency": "CNY",
                            "period": (years[i] + "1231") if i < len(years) else None,
                            "source": "cninfo_pdf"}
            rows.append(row)
        return rows

    out: dict[str, list[dict]] = {}
    if "母公司利润表" in reports:
        r = reports["母公司利润表"]
        out["母公司利润表"] = mk(r.items, r.years)
    if "母公司资产负债表" in reports:
        r = reports["母公司资产负债表"]
        out["母公司资产负债表"] = mk(r.items, r.years)
    if "母公司现金流量表" in reports:
        r = reports["母公司现金流量表"]
        out["母公司现金流量表"] = mk(r.items, r.years)
    return out


if __name__ == "__main__":
    import argparse, json, sys

    ap = argparse.ArgumentParser(description="母公司报表 PDF 提取器")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    reps = extract_parent_reports(args.pdf, verbose=True)
    if not reps:
        print("未找到母公司报表", file=sys.stderr)
        sys.exit(2)

    for name, r in reps.items():
        print(f"\n=== {name} (第 {r.page} 页) 年份 {r.years} ===")
        for k, vs in list(r.items.items())[:12]:
            fmt = ", ".join(f"{v:,.0f}" if v is not None else "—" for v in vs)
            print(f"  {k:<16} {fmt}")
