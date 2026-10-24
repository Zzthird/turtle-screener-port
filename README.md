# TurtlePi

> ## 来源与授权状态
>
> **本项目基于 [terancejiang/Turtle_investment_framework](https://github.com/terancejiang/Turtle_investment_framework) v2.x 移植与改造。**
>
> 上游项目当前**未附加开源许可证**（`license: null`）。本仓库用于个人学习与技术验证，**不作商业用途**。
> 若上游作者对二次分发有不同意见，请通过 issue 联系，我会立即调整。
>
> **上游原有内容**：`skill/shared/`、`skill/strategies/`、`skill/references/` 下的框架脚本与文档（74 个文件作为基线）。
>
> **本仓库中由本人完成的工作**：
> - 将整套框架从 Claude Code 环境移植到本地 **pi** AI 编程助手环境（命令模板、子代理编排、路径适配）
> - **数据层重写**：把付费的 Tushare 数据源替换为新浪 / 同花顺 / 雪球 / 智兔 / 巨潮 PDF 五个免费源，含多源轮换与失败降级
> - **请求限流与断点续跑**：单源节流、连续失败自动暂停、Tier2 进度持久化
> - **数据校验与纠错**：科目名口径对齐、门槛生效验证、误差回归（详见下方「与上游的差异」）
>
> 开发方式说明：代码由 AI 编程助手根据本人的需求定义与验收结果生成，本人负责数据源调研、接口实测、结果校验与取舍决策。

---

把 **龟龟投资框架 v2.x** 从 Claude Code 移植到 **pi** 的开发工作区。

> 主干唯一：`terancejiang/Turtle_investment_framework` (v2.1-beta)
> 不混拼 v0.15（`Stock_Analyze_Prompts`），该项目仅作方法论参考。

---

## 为什么做

原框架依赖 Claude Code 的三样东西：

| Claude Code 能力 | 用法 | pi 替代 |
|---|---|---|
| `.claude/commands/*.md` | slash command 入口 | Prompt Templates |
| `Task(subagent_type=...)` | 子代理编排 | subagent 扩展 |
| `AskUserQuestion` | 交互补全 | question 扩展 / 直接对话 |

**好消息**：框架 90% 的价值在 Python 脚本里（确定性计算、网格预计算），那部分与 Harness 完全解耦，**原样可用**。只有 prompt 编排层需要改。

---

## 目录结构

```
D:\Codes\TurtlePi\
├── README.md                  # 本文件
├── AGENTS.md                  # 项目初始化指南（工作目录约定 / API / 坑）
├── skill/                     # pi skill 源（改完同步到 ~/.pi/agent/skills/turtle-analysis/）
│   ├── SKILL.md               # 入口：流程 / 公式 / 硬约束
│   ├── scripts/               # v2.x 脚本 + 自研采集器/选股器
│   │   ├── data_collector.py       ★ 17 段统一采集入口
│   │   ├── derived_metrics_local.py §17 网格预计算
│   │   ├── quality_control.py       CM§1-5 确定性预算
│   │   ├── cninfo_downloader.py     巨潮年报下载
│   │   ├── pdf_parent_extractor.py  母公司报表提取（覆盖 7/7）
│   │   ├── screener_core.py        ★ Tier1/Tier2 选股主逻辑
│   │   ├── screener_backend.py     免费后端（快照/分红/质押/负债率）
│   │   ├── screener_config.py      筛选阈值配置
│   │   └── datasource/             ★ 数据层（防腐层）
│   │       ├── schema.py           Value + 数据集定义（中文业务语义）
│   │       ├── mapper.py           字段双向映射
│   │       ├── api.py              get() / compare()
│   │       └── adapters/           akshare / zzshare / tushare
│   ├── shared/                # [已拷] 通用定性模块，28 文件 200 KB
│   ├── strategies/            # [已拷] 龟龟 + 估值策略，15 文件 135 KB
│   └── references/            # [已拷] v2.x README / CHANGELOG / requirements
├── agents/                    # pi subagent 定义（4 个，已装到 ~/.pi/agent/agents/）
├── prompts/                   # pi 命令模板（3 个，已装到 ~/.pi/agent/prompts/）
├── notes/                     # 移植笔记、实测记录（必读）
└── output/                    # 分析结果
```

### 资产来源

| 来源 | 内容 | 去向 |
|---|---|---|
| `Turtle_investment_framework` v2.x | `scripts/` `shared/` `strategies/` | `skill/` |
| 同上 | README / CHANGELOG_V2 / requirements | `skill/references/` |
| 本地克隆缓存 | `D:\tmp\Turtle_investment_framework` | — |

---

## 移植计划

### 已完成
- [x] **Step 1** 搭建目录骨架
- [x] **Step 2** 拷贝 v2.x 资产（`scripts/` `shared/` `strategies/`）

### 已完成（全部 8 步）

- [x] **Step 1–2** 目录骨架 + 拷贝 v2.x 资产
- [x] **Step 3** 路径与环境适配（Windows / pi / venv）
- [x] **Step 4** 外部依赖替换 —— **A 股已不依赖 Tushare token**
- [x] **Step 5** `skill/SKILL.md`（含硬约束：有 §17 网格禁止手算、数字必须溯源）
- [x] **Step 6** 4 个项目 agent（qualitative / quant / valuation / auditor）
- [x] **Step 7** 3 个命令模板（`/turtle-analysis` `/business-analysis` `/valuation`）
- [x] **Step 8** 端到端验证（600887 伊利，与原仓库结论一致）

**Step 4 实际做的工作**（比预想重得多）：

| 原依赖 | 实际情况 |
|---|---|
| `TUSHARE_TOKEN` | ❌ 不是硬门槛 —— 已用 AkShare + zzshare + 智兔 + 雪球 + 巨潮 PDF 完全替代 |
| 母公司报表（§3P/§4P） | Tushare 也无，改用巨潮年报 PDF 提取，覆盖 **7/7**（含银行、无框线报表） |

---

## 后续开发（移植之后）

### 数据层多源化

`datasource/adapters/akshare_adapter.py` 里三类报表（利润表 / 资产负债表 / 现金流量表）
各接 **5 个源**，彼此数值一致（见下方对照）。

| 源 | 三表 | 耗时 | 已知局限 |
|---|---|---|---|
| **新浪 openapi** | ✅ | 0.14–0.24s | — （主力源，科目最齐，含银行） |
| **同花顺**（AkShare 封装） | ✅ | 0.08–0.26s | 财务费用需用【利息支出 − 利息收入】合成；无「处置固定资产收回」 |
| **雪球** | ✅ | 0.11–0.19s | 需 `xq_a_token`，会过期 |
| **智兔** | ✅ | 0.24–0.71s | 额度小（已 4-token 轮换 + 429 自动换） |
| **新浪 AkShare**（旧路径） | ✅ | 0.4–8.8s | 慢，末位兜底 |
| 东财 | ⚠️ | 1.3–4.1s | 现金流表在 `dataset_call` 链路下除首列外全空，**当前不在轮换池** |

单源一致性验证（AA，单位百万元）：

| 股票 | 新浪 | 同花顺 | 雪球 | 智兔 | 新浪Ak |
|---|---|---|---|---|---|
| 五粮液 000858 | 35,262 | 35,241 | 35,262 | 35,262 | 35,262 |
| 茅台 600519 | 105,009 | 105,004 | 105,009 | 105,009 | 105,009 |

### 轮换调度

Tier2 逐股分析时，三表从不同源**起点轮换**（`_rotated(pool, idx)`，
`idx` 经 `contextvars` 传递），失败自动降级。

- 首次全量跑：每源承担 ~333/5 次
- 重跑：全部命中磁盘缓存 → **0 请求**

### 断点续跑

Tier2 每算完一只立即写 `output/.screener_cache/tier2_progress.json`；
中断后重跑自动跳过已完成（含被否决的）。限流时连续 N 只失败会
保存进度并优雅退出：

```
SCREENER_TIER2_RESUME=0        强制重跑
SCREENER_TIER2_CKPT=<path>     进度文件路径
SCREENER_MAX_CONSEC_FAIL=5     连续失败阈值
```

---

## 不动的东西（约 90% 文件）

- 全部框架 Python 脚本（`derived_metrics.py` / `valuation_engine.py` 等）
- `shared/` 与 `strategies/` 的全部 md 内容
- 920 个测试（`tests/`，本次未拷，如需可补）

---

## 真实成本

| 项 | 实际 |
|---|---|
| 拷文件 + 改路径 | ~20 分钟 |
| 装扩展 + 写 agent | ~1 小时 |
| **拿 Tushare token** | ❌ **不需要** —— 已用免费源完全平替 |
| **母公司报表（§3P/§4P）** | 免费源全无 → 自写巨潮 PDF 提取，覆盖 7/7 |
| **调通 + 验证** | **最大成本**（数据源字段对齐、口径校验） |
| **多源轮换 + 断点续跑** | 后续开发，为跑全市场选股服务 |

克隆省的是"设计架构"和"写脚本"，省不掉"配环境"和"验证"。

---

## 前置条件

1. **Python ≥ 3.10**（本机 conda base = 3.12.15 ✅）
2. **TUSHARE_TOKEN**：❌ **不需要**（A 股）
3. `.env`（可选）：`ZHITU_TOKEN` / `ZHITU_TOKENS` / `XQ_TOKEN`
4. pi subagent 扩展（✅ 已装 `pi-subagents`）

---

## 参考

- 主干仓库：https://github.com/terancejiang/Turtle_investment_framework
- 方法论参考（不含代码，仅用于理解思路）：https://terancejiang.github.io/Stock_Analyze_Prompts/
- 视频《龟龟策略 agent Workflow 的设计和使用》：https://www.bilibili.com/video/BV1kzwczaEef/
- 数据源调研：`notes/data-source-research.md` / `notes/datasource-full-test.md`

---

*最后更新：多源轮换 + 断点续跑 + 全量 Tier2 跑通（931 只 → 390 只通过）*

---

## 与上游的差异

本人相对上游 `Turtle_investment_framework` v2.x 的改动（按 `git diff` 统计）：

| 类别 | 数量 |
|---|---|
| 新增文件 | 52 |
| 修改的上游文件 | 17 |
| 删除文件 | 0 |

主要改动集中在以下模块：

- **`skill/scripts/screener_core.py`** —— Tier1/Tier2 选股主逻辑，门槛判定、否决门、断点续跑
- **`skill/scripts/screener_backend.py`** —— 免费数据源后端（快照 / 分红 / 质押 / 负债率）
- **`skill/scripts/datasource/adapters/akshare_adapter.py`** —— 三表多源接入与轮换
- **`skill/scripts/rate_limit.py`** —— 请求节流与连续失败保护
- **`skill/scripts/pdf_parent_extractor.py`** —— 巨潮年报 PDF 母公司报表提取（新增）
- **`notes/`** —— 数据源实测与验证记录（新增）

`skill/shared/`、`skill/strategies/`、`skill/references/` 下的内容保持上游原样。
