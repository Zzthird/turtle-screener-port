"""逐段路由：为每个数据段选择数据源，按优先级尝试。

设计要点：
1. 优先级**按数据集配置**，不是全局一条线
   （A股财报 AkShare 优先；母公司报表只有 Tushare）
2. 单源覆盖不全 → 逐段选源
3. 每段记录实际来源，供审计追溯

实测依据见 notes/datasource-full-test.md / datasource-others-test.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 路由表：数据段 -> 数据源优先级
# ---------------------------------------------------------------------------

ROUTING: dict[str, list[str]] = {
    # 段号: [源优先级从高到低]
    "1":   ["akshare", "zzshare", "tushare"],       # 基本信息
    "2":   ["akshare", "tushare"],                  # 市场行情（52周）
    "3":   ["akshare", "zzshare", "tushare"],       # 合并利润表
    "3P":  ["tushare"],                             # 母公司利润表 ← 唯一源
    "4":   ["akshare", "zzshare", "tushare"],       # 合并资产负债表
    "4P":  ["tushare"],                             # 母公司资产负债表 ← 唯一源
    "5":   ["akshare", "tushare"],                  # 合并现金流量表（ak 字段最全）
    "6":   ["akshare", "tushare"],                  # 分红历史
    "11":  ["akshare", "tushare"],                  # 十年周线
    "12":  ["akshare", "zzshare", "tushare"],       # 财务指标
    "14":  ["akshare", "tushare"],                  # 无风险利率
    "15":  ["akshare", "tushare"],                  # 股份回购
    "16":  ["akshare", "tushare"],                  # 股权质押
    "9":   ["akshare", "tushare"],                  # 主营业务构成
    "7":   ["akshare", "tushare"],                  # 股东与治理（十大股东）
    "17":  ["local"],                               # 衍生指标（本地计算）
}

# 数据段 -> datasource 数据集名（None 表示无对应数据集，如母公司报表/本地计算）
SECTION_DATASET: dict[str, Optional[str]] = {
    "1":  "股票基础信息",
    "2":  "每日指标",
    "3":  "利润表",
    "3P": None,                # 母公司利润表：仅 Tushare，未接入数据层
    "4":  "资产负债表",
    "4P": None,                # 母公司资产负债表：同上
    "5":  "现金流量表",
    "6":  "分红",
    "11": "周线行情",
    "12": "财务指标",
    "14": "无风险利率",
    "15": "股份回购",
    "16": "股权质押",
    "9":  "主营业务构成",
    "7":  "十大股东",
    "17": None,                # 衍生指标：本地计算
}

# 段号 -> 显示名称（与 tushare_modules/assembly.py 一致）
SECTION_TITLE: dict[str, str] = {
    "1":  "1. 基本信息",
    "2":  "2. 市场行情",
    "3":  "3. 合并利润表（5年）",
    "3P": "3P. 母公司利润表",
    "4":  "4. 合并资产负债表（5年）",
    "4P": "4P. 母公司资产负债表",
    "5":  "5. 合并现金流量表 + FCF",
    "6":  "6. 分红历史",
    "11": "11. 十年周线行情",
    "12": "12. 财务指标",
    "14": "14. 无风险利率",
    "15": "15. 股份回购",
    "16": "16. 股权质押",
    "9":  "9. 主营业务构成",
    "7":  "7. 股东与治理 (部分)",
    "13": "13. 风险警示",
    "17": "17. 衍生指标预计算",
}

# 降级容忍：缺失时不中止分析，仅标注可信度降低
DEGRADABLE: set[str] = {"3P", "4P", "15", "16", "6", "9"}


@dataclass
class SectionResult:
    """单个数据段的取数结果。"""

    section: str
    dataset: str
    ok: bool
    source: Optional[str] = None
    rows: int = 0
    attempts: list[tuple[str, str]] = field(default_factory=list)   # [(源, 错误)]
    degradable: bool = False

    @property
    def status(self) -> str:
        if self.ok:
            return f"✅ {self.source} ({self.rows} 行)"
        if self.degradable:
            return "⚠️ 降级（可容忍缺失）"
        return "❌ 失败（必需段）"

    def reason(self) -> str:
        if self.ok or not self.attempts:
            return ""
        return "; ".join(f"{s}: {e[:60]}" for s, e in self.attempts)


def sources_for(section: str) -> list[str]:
    return ROUTING.get(section, ["akshare", "tushare"])


def dataset_for(section: str) -> Optional[str]:
    return SECTION_DATASET.get(section)


def title_for(section: str) -> str:
    return SECTION_TITLE.get(section, f"{section}.")


def is_degradable(section: str) -> bool:
    return section in DEGRADABLE
