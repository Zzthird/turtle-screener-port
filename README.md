# TurtlePi

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
├── skill/                     # pi skill 骨架（改通后装到 ~/.pi/agent/skills/）
│   ├── scripts/               # [已拷] v2.x Python 脚本，22 文件 453 KB
│   ├── shared/                # [已拷] 通用定性模块，28 文件 200 KB
│   ├── strategies/            # [已拷] 龟龟 + 估值策略，15 文件 135 KB
│   └── references/            # [已拷] v2.x README / CHANGELOG / requirements
├── agents/                    # [待写] pi subagent 定义
├── prompts/                   # [待写] pi 命令模板
├── notes/                     # [待写] 移植笔记、差异记录
└── output/                    # [待写] 分析结果
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

### 待执行

**Step 3 — 路径与环境适配**
| 原写法 | pi / Windows 写法 |
|---|---|
| `.venv/bin/python` | `.venv\Scripts\python.exe` |
| `bash init.sh` | 手动 `python -m venv .venv` + `pip install -r requirements.txt` |
| `Bash("mkdir -p ...")` | pi 的 powershell 工具 |
| `/sessions/*/mnt/uploads/` | 用户直接提供路径 |

**Step 4 — 外部依赖替换**
| 原依赖 | 替换方案 |
|---|---|
| `snowball-report-downloader` 插件 | `scripts/download_report.py` |
| `Skill(...)` 调用 | pi skill 机制 |
| `TUSHARE_TOKEN` | **硬门槛，必须配置** |

**Step 5 — Skill 化**
- 写 `skill/SKILL.md`：入口 + 流程 + 硬约束
  - 硬约束：有 §17 网格时禁止手算，只查表选行
  - 硬约束：支撑性数字必须带 `[src: ...]` 溯源

**Step 6 — Subagent 编排**
| 龟龟角色 | pi agent 文件 |
|---|---|
| Agent B 定量 | `agents/quant.md` |
| Agent C 估值 | `agents/valuation.md` |
| 定性分析 | `agents/qualitative.md` |
| clean-room 复核 | `agents/auditor.md` |

装 subagent 扩展：
```powershell
Copy-Item "D:\npm\node_modules\@agegr\pi-web\node_modules\@earendil-works\pi-coding-agent\examples\extensions\subagent\*" `
          "C:\Users\Administrator\.pi\agent\extensions\subagent\" -Recurse
```

**Step 7 — 命令模板**
- `prompts/turtle-analysis.md` → `/turtle-analysis 600887`
- `prompts/business-analysis.md` → `/business-analysis 600887`
- `prompts/valuation.md` → `/valuation 600887`

**Step 8 — 验证**
- 跑 600887（伊利股份）
- 与原仓库报告逐项对照：结论 / GG / II / 安全边际 / 因子1A 六项
- 同一标的跑两次，确认数字稳定（v2.x 特性：逐字节一致，仅时间戳不同）

---

## 不动的东西（约 90% 文件）

- 全部 Python 脚本（`derived_metrics.py` / `screener_core.py` / `valuation_engine.py` 等）
- `shared/` 与 `strategies/` 的全部 md 内容
- 920 个测试（`tests/`，本次未拷，如需可补）

---

## 真实成本

| 项 | 预估 |
|---|---|
| 拷文件 + 改路径 | ~20 分钟 |
| 装扩展 + 写 agent | 1–2 小时 |
| **拿 Tushare token** | **未知，需积分，可能卡几天** |
| **调通 + 验证结果** | **几小时（真正的成本）** |

克隆省的是"设计架构"和"写脚本"，省不掉"配环境"和"验证"。

---

## 前置条件

1. **Python ≥ 3.10**（本机 conda base = 3.12.15 ✅）
2. **TUSHARE_TOKEN**（⚠️ 未配置，硬门槛）
3. pi subagent 扩展（⚠️ 未安装）

---

## 参考

- 主干仓库：https://github.com/terancejiang/Turtle_investment_framework
- 方法论参考（不含代码，仅用于理解思路）：https://terancejiang.github.io/Stock_Analyze_Prompts/
- 视频《龟龟策略 agent Workflow 的设计和使用》：https://www.bilibili.com/video/BV1kzwczaEef/

---

*最后更新：Step 1、2 完成*
