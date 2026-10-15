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
# 注：银行年报用「本行」/「公司」代替一般企业的「母公司」
REPORTS = {
    "母公司资产负债表": ["母公司资产负债表", "本行资产负债表", "公司资产负债表"],
    "母公司利润表": ["母公司利润表", "本行利润表", "公司利润表"],
    "母公司现金流量表": ["母公司现金流量表", "本行现金流量表", "公司现金流量表"],
}

# 标题变体（带"续"、换行、空格）
_TITLE_NOISE = re.compile(r"[\s　\u3000\-—·]+")

# 标题必须位于页面靠前位置（排除审计报告/附注里的提及）
_TITLE_MAX_POS = 400

# 干扰页特征（审计报告 / 附注提及）
_NOISE_PAGE = ["审计报告", "审阅报告", "毕马威华振审字", "天职业字", "XYZH/"]

# 单位识别（部分公司用百万元，如中国神华）
_UNIT_PATTERNS = [
    (re.compile(r"单位[：:]\s*人民币?百万元"), "百万元"),
    (re.compile(r"金额单位[：:]\s*人民币?百万元"), "百万元"),
    (re.compile(r"单位[：:]\s*万元"), "万元"),
    (re.compile(r"单位[：:]\s*元"), "元"),
]

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

# ============ 银行专用科目映射 ============
BANK_BALANCE_ITEMS = {
    "现金": "货币资金",
    "现金及存放中央银行款项": "货币资金",
    "贵金属": "贵金属",
    "存放中央银行款项": "存放央行款项",
    "存放同业和其他金融机构款项": "存放同业",
    "拆出资金": "拆出资金",
    "衍生金融资产": "衍生金融资产",
    "买入返售金融资产": "买入返售",
    "发放贷款和垫款": "发放贷款及垫款",
    "贷款和垫款": "发放贷款及垫款",
    "金融投资": "金融投资",
    "交易性金融资产": "交易性金融资产",
    "债权投资": "债权投资",
    "其他债权投资": "其他债权投资",
    "长期股权投资": "长期股权投资",
    "固定资产": "固定资产",
    "在建工程": "在建工程",
    "无形资产": "无形资产",
    "商誉": "商誉",
    "递延所得税资产": "递延所得税资产",
    "其他资产": "其他资产",
    "资产总计": "总资产",
    "资产合计": "总资产",
    "向中央银行借款": "向央行借款",
    "同业及其他金融机构存放款项": "同业存放",
    "拆入资金": "拆入资金",
    "衍生金融负债": "衍生金融负债",
    "卖出回购金融资产款": "卖出回购",
    "吸收存款": "吸收存款",
    "应付职工薪酬": "应付职工薪酬",
    "应交税费": "应交税费",
    "应付债券": "应付债券",
    "递延所得税负债": "递延所得税负债",
    "其他负债": "其他负债",
    "负债合计": "总负债",
    "归属于本行股东权益": "归母权益",
    "归属于本行股东权益合计": "归母权益",
    "归属于母公司股东权益合计": "归母权益",
    "股本": "实收资本",
    "资本公积": "资本公积",
    "其他综合收益": "其他综合收益",
    "盈余公积": "盈余公积",
    "一般风险准备": "一般风险准备",
    "未分配利润": "未分配利润",
    "少数股东权益": "少数股东权益",
    "股东权益合计": "所有者权益合计",
    "所有者权益合计": "所有者权益合计",
    "负债及股东权益总计": "负债及权益总计",
    "负债和股东权益总计": "负债及权益总计",
}

BANK_INCOME_ITEMS = {
    "营业收入": "营业收入",
    "利息收入": "利息收入",
    "利息支出": "利息支出",
    "净利息收入": "净利息收入",
    "手续费及佣金收入": "手续费收入",
    "手续费及佣金支出": "手续费支出",
    "手续费及佣金净收入": "手续费净收入",
    "其他净收益": "其他净收益",
    "业务及管理费": "业务管理费",
    "营业税金及附加": "营业税金",
    "资产减值损失": "资产减值损失",
    "信用减值损失": "信用减值损失",
    "营业利润": "经营利润",
    "利润总额": "利润总额",
    "所得税费用": "所得税",
    "净利润": "净利润",
    "归属于本行股东的净利润": "归母净利润",
    "归属于母公司股东的净利润": "归母净利润",
    "少数股东损益": "少数股东损益",
}

# 银行专用映射（按报表名覆盖）
BANK_ITEM_MAPS = {
    "母公司资产负债表": BANK_BALANCE_ITEMS,
    "母公司利润表": BANK_INCOME_ITEMS,
    "母公司现金流量表": CASHFLOW_ITEMS,
}

# 银行标识（页面含这些词则启用银行科目映射）
BANK_MARKERS = ("银行", "本行", "吸收存款", "发放贷款")

_NUM = re.compile(r"^-?[\d,]+\.?\d*$")
_YEAR = re.compile(r"(20\d{2})")


@dataclass
class ReportTable:
    name: str
    page: int
    years: list[str] = field(default_factory=list)     # ["2024", "2023"]
    items: dict[str, list[Optional[float]]] = field(default_factory=dict)
    unit: str = "元"                                    # 原始单位（需转换）


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


def _detect_unit(text: str) -> str:
    """从页面文本识别报表单位。实测：中国神华用百万元，其余多为元。"""
    for pat, unit in _UNIT_PATTERNS:
        if pat.search(text):
            return unit
    return "元"


def _has_title(text: str, name: str) -> bool:
    """标题匹配：标题必须**独占一行**（避免审计报告/附注中夹在句子里的提及）。

    支持"母公司资产负债表（续）"、"本行资产负债表"（银行）。
    """
    kws = [_TITLE_NOISE.sub("", k) for k in REPORTS.get(name, [name])]
    for line in text.splitlines():
        flat = _TITLE_NOISE.sub("", line)
        if not flat:
            continue
        for kw in kws:
            # 标题行：以关键词开头，或形如 "2、母公司资产负债表"
            if flat.startswith(kw) or flat.lstrip("0123456789、.（）()").startswith(kw):
                # 排除正文里引用的句子（行太长）
                if len(flat) <= len(kw) + 12:
                    return True
    return False


def _guess_report_year(text: str) -> Optional[int]:
    """从页面文本猜报告年（如 "2024年12月31日"）。"""
    m = re.search(r"(20\d{2})\s*年\s*12\s*月\s*31\s*日", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(20\d{2})\s*年?\s*1[]—\-~至]\s*12\s*月", text)
    if m:
        return int(m.group(1))
    return None


def _is_toc_page(text: str) -> bool:
    """目录页判定：点线引导符 或 典型目录特征。

    注意：银行报表科目名短、行尾多为数字，易误判。
    因此若页首含明确的报表标题，一律不视为目录。
    """
    head = text[:120]
    for kws in REPORTS.values():
        for k in kws:
            if k in head:
                return False          # 明确标题 -> 不是目录页
    if "......" in text or "…" in text or "‧‧" in text:
        return True
    # 短页面 + 行尾多为数字（目录特征）
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) > 8:
        tail_num = sum(1 for l in lines if l.rstrip().endswith(tuple("0123456789")))
        if tail_num / len(lines) > 0.7 and sum(len(l) for l in lines) / len(lines) < 30:
            return True
    return False


# 表格提取策略（实测）
#   lines: 默认，适合有框线的报表
#   text : 适合无框线报表（如中国神华），按文字位置推断列
_TABLE_SETTINGS = [
    {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    {"vertical_strategy": "text", "horizontal_strategy": "text"},
]


def _extract_tables(page) -> list[list[list[str]]]:
    """多策略表格提取（lines 优先，text 回退）。"""
    for st in _TABLE_SETTINGS:
        try:
            tbs = [t for t in page.extract_tables(table_settings=st) if t and len(t) >= 3]
            if tbs:
                return tbs
        except Exception:                           # noqa: BLE001
            continue
    return []


def _find_header_row(table: list[list[str]]) -> tuple[int, list[str]]:
    """定位表头行，返回 (行号, 年份列表)。

    常见表头形式：
      项目 | 附注 | 2024年12月31日 | 2023年12月31日   -> 年份
      项目 | 期末余额 | 期初余额                       -> 无年份，回退
    """
    for i, row in enumerate(table[:8]):
        cells = [(c or "").strip() for c in row]
        years = []
        for c in cells:
            m = _YEAR.search(c)
            if m and ("年" in c or "月" in c or c.strip().startswith("20")):
                years.append(m.group(1))
        if len(years) >= 1:
            return i, years
        # 跨行：本行只有"附注"，下行才是年份
        if i + 1 < len(table):
            nxt = [(c or "").strip() for c in table[i + 1]]
            ys = [_YEAR.search(c).group(1) for c in nxt
                  if _YEAR.search(c) and ("年" in c or "月" in c or c.startswith("20"))]
            if ys:
                return i + 1, ys
    # 无年份：找"期末余额/期初余额"，用占位符（后续由 PDF 元信息补年份）
    for i, row in enumerate(table[:8]):
        flat = "".join((c or "") for c in row)
        if "期末余额" in flat or "期末数" in flat or "年末余额" in flat:
            return i, ["T", "T-1"]
    return -1, []


def _label_col(table: list[list[str]]) -> int:
    """定位科目名称所在列（部分报表第一列全空）。"""
    # 看表头行哪个单元格是"项目"
    for row in table[:8]:
        for ci, c in enumerate(row):
            t = (c or "").strip()
            if t in ("项目", "项　目", "科目", "资产", "负债和股东权益"):
                return ci
    # 回退：找含文字最多的列（前 20 行）
    widths = {}
    for row in table[:20]:
        for ci, c in enumerate(row):
            s = (c or "").strip()
            if s and not _NUM.match(s.replace(",", "")):
                widths[ci] = widths.get(ci, 0) + 1
    if widths:
        return max(widths, key=widths.get)
    return 0
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
                   years: list[str], bank: bool = False) -> dict[str, list[Optional[float]]]:
    """从多张表（含跨页续表）提取科目值。bank=True 时用银行科目映射。"""
    all_items = (BANK_ITEM_MAPS if bank else ITEM_MAPS)[name]
    items: dict[str, list[Optional[float]]] = {}
    for best in tables:
        lc = _label_col(best)                 # 科目所在列
        for row in best:
            if not row or len(row) <= lc:
                continue
            label = str(row[lc] or "")
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
                    vals = [_to_num(str(c or "")) for c in row[lc + 1:]]
                    nums = [v for v in vals if v is not None]
                    if nums:
                        items[biz] = nums[:len(years)] if years else nums
                    break
    return items


# 数据行正则：科目名 + 可选附注 + 1~2 个数值
# 实测招行: '利息收入 42 347,786 351,273' / '净利息收入 205,493 207,705'
_LINE_NUM = r"[-−]?\(?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?"
_ROW_RE = re.compile(
    r"^(?P<label>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z（）()、：:·\s]{1,40}?)\s+"
    r"(?:(?P<note>[\u4e00-\u9fa5A-Za-z0-9（）()·\.]{1,12})\s+)?"
    r"(?P<v1>%s)"
    r"(?:\s+(?P<v2>%s))?\s*$" % (_LINE_NUM, _LINE_NUM)
)


def _parse_text_rows(text: str, all_items: dict, years: list[str]) -> dict[str, list[Optional[float]]]:
    """从纯文本行解析报表（应对无框线/列合并的 PDF）。

    格式：科目 [附注] 数值1 [数值2]
    """
    items: dict[str, list[Optional[float]]] = {}
    ncol = max(len(years), 1)
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 6:
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        label = m.group("label").replace(" ", "").replace("　", "")
        label = re.sub(r"^[一二三四五六七八九十]+、", "", label)
        label = re.sub(r"^(减|加|其中|其中：|及)：?", "", label).replace("：", "").strip()
        if not label:
            continue
        def _n(s):
            if not s:
                return None
            neg = s.startswith("(") or s.startswith("−") or s.startswith("-")
            t = s.strip("()（）").replace(",", "").replace("−", "-")
            try:
                v = float(t)
                return -v if (neg and v > 0) else v
            except ValueError:
                return None
        vals = [_n(m.group("v1")), _n(m.group("v2"))]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        for anchor, biz in all_items.items():
            if biz in items:
                continue
            if label == anchor or label.startswith(anchor):
                items[biz] = (vals + [None] * ncol)[:ncol]
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
                if not _has_title(text, name):
                    continue
                # 页级过滤：排除目录页
                if _is_toc_page(text):
                    continue

                tables = _extract_tables(page)
                if not tables:
                    continue

                hi, years = -1, []
                for t in tables:
                    hi, years = _find_header_row(t)
                    if years:
                        break
                if not years:
                    continue

                unit = _detect_unit(text)
                # 银行年报：启用银行科目映射
                bank = any(m in text for m in BANK_MARKERS)

                # 跨页续表：向后扫描，直到出现下一张报表标题
                chain = list(tables)
                j = pno + 1
                while j < n_pages and j <= pno + 4:
                    nt = pdf.pages[j].extract_text() or ""
                    # 任何母公司报表标题（包括本报表的"（续）"会继续）
                    if any(_has_title(nt, k) for k in REPORTS) and not _has_title(nt, name):
                        break
                    if _is_toc_page(nt):
                        break
                    more = _extract_tables(pdf.pages[j])
                    if not more:
                        break
                    chain.extend(more)
                    j += 1

                items = _collect_items(chain, name, years, bank=bank)

                # 银行报表：表格提取易把「附注」列误当数值 -> 优先用文本行解析
                use_text = bank
                if use_text or len(items) < 4:
                    all_items = (BANK_ITEM_MAPS if bank else ITEM_MAPS)[name]
                    txt_items = _parse_text_rows(text, all_items, years)
                    j2 = pno + 1
                    while j2 < n_pages and j2 <= pno + 4:
                        nt = pdf.pages[j2].extract_text() or ""
                        if any(_has_title(nt, k) for k in REPORTS) and not _has_title(nt, name):
                            break
                        if _is_toc_page(nt):
                            break
                        more = _parse_text_rows(nt, all_items, years)
                        if not more:
                            break
                        for k, v in more.items():
                            txt_items.setdefault(k, v)
                        j2 += 1
                    if bank or len(txt_items) > len(items):
                        if txt_items:
                            items = txt_items
                            if verbose:
                                print(f"    （文本行解析 → {len(items)} 科目）")

                if items:
                    # 占位年份 T/T-1 -> 用 PDF 里的报告年份回填
                    if years and years[0] == "T":
                        ry = _guess_report_year(text) or _guess_report_year(pdf_path)
                        if ry:
                            years = [str(ry), str(ry - 1)]
                    found[name] = ReportTable(name=name, page=pno + 1,
                                              years=years, items=items, unit=unit)
                    if verbose:
                        span = f"第 {pno+1}" + (f"-{j}" if j > pno + 1 else "") + " 页"
                        print(f"  找到 {name} @ {span}，{len(items)} 科目，年份 {years}，单位 {unit}")

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
    数值统一转为【元】（供下游 /1e6 成百万元）。
    """
    _SCALE = {"元": 1.0, "万元": 1e4, "百万元": 1e6}

    def mk(items: dict, years: list[str], unit: str) -> list[dict]:
        scale = _SCALE.get(unit, 1.0)
        rows = []
        n = len(years) if years else 1
        for i in range(n):
            row: dict = {}
            for biz, vals in items.items():
                v = vals[i] if i < len(vals) else None
                if v is not None:
                    v = v * scale               # 归一化到元
                row[biz] = {"value": v, "unit": "元", "currency": "CNY",
                            "period": (years[i] + "1231") if i < len(years) else None,
                            "source": "cninfo_pdf"}
            rows.append(row)
        return rows

    out: dict[str, list[dict]] = {}
    for name in ("母公司利润表", "母公司资产负债表", "母公司现金流量表"):
        r = reports.get(name)
        if r:
            out[name] = mk(r.items, r.years, r.unit)
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
