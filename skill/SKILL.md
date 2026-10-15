---
name: turtle-analysis
description: 龟龟投资框架（Turtle Investment Framework）四因子基本面分析，覆盖 A股/港股/美股。
  用 Tushare 采集确定性数据，Python 脚本完成穿透回报率、估值与选股筛选，LLM 只做定性判断与解释。
  当用户要求个股基本面分析、穿透回报率计算、价值陷阱排查、深价值选股、
  或提到"龟龟策略""龟龟框架""turtle"时使用。
---

# 龟龟投资框架（v2.x → pi 移植版）

混合架构：**Python 做确定性计算，LLM 做定性判断**。

## 环境

| 项 | 值 |
|---|---|
| 工作目录 | `D:\Codes\TurtlePi\skill` |
| Python | `.venv\Scripts\python.exe`（Python 3.12） |
| 脚本 | `scripts/` |
| 策略 prompt | `strategies/` `shared/` |
| 输出 | `output/{代码}_{公司}/` |

**调用脚本一律用**：`.venv\Scripts\python.exe scripts\xxx.py ...`

## 前置条件（必须先检查）

1. **`TUSHARE_TOKEN`** — 未配置则数据层不可用（仅能 yfinance 降级）
2. 脚本以 **CLI 方式**运行，不要 `import scripts.xxx`

## 三条流水线

```
/business-analysis {code}   ← ① 定性（必须先跑）
     产出 qualitative_report.md + data_pack_market.md
                ↓
/turtle-analysis {code}     ← ② 定量 + 估值（依赖 ①）
     产出 {公司}_{代码}_分析报告.md
                ↓
/valuation {code}           ← ③ 可选，独立估值
```

## 核心流程（turtle-analysis）

```
Step A  市场数据刷新
  .venv\Scripts\python.exe scripts\tushare_collector.py \
     --code {ts_code} --output output/{code}_{company}/data_pack_market.md --refresh-market

Phase 3.1  Agent B（quant）：Step 0 数据校验 + 11 步穿透回报率
  → output/{code}_{company}/phase3_quantitative.md

Phase 3.2  Agent C（valuation）：估值 + 报告组装
  → output/{code}_{company}/{公司}_{代码}_分析报告.md
```

## 核心公式

```
穿透回报率 = [基准值 × 支付率 M × (1 − 税率 Q) + 回购 O] / 市值
  粗算 R  : 基准值 = C（归母净利润）
  精算 GG : 基准值 = AA（真实可支配现金结余）

门槛 II = A股 max(3.5%, Rf+2%) | 港股 max(5%, Rf+3%) | 美股 max(5%, Rf_US+3%)
安全边际 KK = GG − II     → 仓位矩阵
目标买入价 = 当前股价 × GG / II
```

## 硬约束（不可违反）

1. **有 §17 网格时禁止手算** —— §17.10–§17.13 已由 Python 预计算（M 三重校验、回报率网格、G 系数网格、λ 敏感性）。Agent 只能**查表选行并说明理由**，不得重算。仅当对应小节缺失才走降级手算路径。
2. **数字必须带溯源** —— 格式 `[src: DP§3]` / `[src: CM§4]` / `[src: 年报P.45]` / `[src: Web]` / `[src: 推断]`。
3. **不编造数据** —— 缺失标注 `⚠️ 数据不可用`，禁用"大约/预计/约为"掩盖。
4. **金额单位百万元**，千位逗号，百分比保留 2 位小数。
5. **不调用外部数据源**（Agent B/C）—— 所有数据来自数据包文件。
6. **禁止向 `strategies/` `shared/` `scripts/` 写入任何文件。**

## 子代理（subagent 扩展）

| 角色 | 定义文件 | 职责 |
|---|---|---|
| Agent B | `~/.pi/agent/agents/quant.md` | Step 0 校验 + 11 步穿透回报率 |
| Agent C | `~/.pi/agent/agents/valuation.md` | 估值 + 报告组装 + 买入/观察判断 |
| 定性 | `~/.pi/agent/agents/qualitative.md` | 6 维度定性分析 |
| 审计 | `~/.pi/agent/agents/auditor.md` | clean-room 独立重算 |

## 详细指令索引

| 要做的事 | 读这个文件 |
|---|---|
| 完整调度逻辑 | `strategies/turtle/coordinator.md` |
| 定量（Agent B） | `strategies/turtle/phase3_quantitative.md` |
| 估值（Agent C） | `strategies/turtle/phase3_valuation.md` |
| 税率/门槛/公式 | `strategies/turtle/references/shared_tables.md` |
| 参数传递 schema | `strategies/turtle/references/factor_interface.md` |
| G系数/λ 锚点 | `strategies/turtle/references/judgment_examples_turtle.md` |
| 定性模块入口 | `shared/qualitative/coordinator.md` |
| 6 维度定性 | `shared/qualitative/qualitative_assessment.md` |
| 定性审计 | `shared/qualitative/agents/cleanroom_audit.md` `numeric_audit.md` |
| 独立估值 | `strategies/valuation/coordinator.md` |

## 脚本索引

| 脚本 | 用途 |
|---|---|
| `tushare_collector.py` | 数据采集（支持 `--refresh-market`） |
| `pdf_preprocessor.py` | 年报 PDF 7 章节提取 |
| `quality_control.py` | 确定性预算指标 → `computed_metrics.md` |
| `derived_metrics.py` | §17 网格预计算（模块） |
| `valuation_engine.py` | DCF/DDM/可比/Graham |
| `screener_core.py` | 两级选股筛选 |
| `report_consistency.py` | 跨段数值冲突扫描 |
| `report_to_html.py` | MD → HTML 仪表盘 |
| `download_report.py` | 年报下载 |

## 异常处理

| 情况 | 处理 |
|---|---|
| `qualitative_report.md` 缺失 | 停止，提示先跑 `/business-analysis` |
| `data_pack_market.md` 缺失 | 停止，同上 |
| `data_pack_report.md` 缺失 | 继续（Agent B 降级，无附注数据） |
| Token 无效 | yfinance fallback |
| Step 0 裁决 `SUPPLEMENT_NEEDED` | 解析补救请求 → WebSearch 补充 → 重跑（最多 1 次） |
| Step 0 裁决 `ABORT` | 通知用户并停止 |

## 版本

主干：`terancejiang/Turtle_investment_framework` v2.1-beta
移植记录：`D:\Codes\TurtlePi\notes\path-audit.md`
