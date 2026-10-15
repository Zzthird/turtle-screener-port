# TurtlePi — 龟龟投资框架（pi 移植版）

> 把 `terancejiang/Turtle_investment_framework` (v2.x) 从 Claude Code 移植到 pi，
> 并**用免费数据源替代 Tushare 付费依赖**。

---

## 项目速览

| 项 | 值 |
|---|---|
| 工作目录 | `D:\Codes\TurtlePi` |
| Python | `D:\Codes\TurtlePi\skill\.venv\Scripts\python.exe`（3.12） |
| 已装 pi skill | `~/.pi/agent/skills/turtle-analysis/` |
| 已装 pi agents | `~/.pi/agent/agents/{quant,valuation,qualitative,auditor}.md` |
| agent 源文件 | 本项目 `agents/`（已纳入版本管理，与 `~/.pi/agent/agents/` 保持同步） |
| 数据源 | **AkShare + zzshare + 巨潮 PDF**（A股无需 Tushare token） |
| 母公司报表覆盖 | **7/7**（含银行、无框线报表） |

**核心设计**：Python 做确定性计算，LLM 只做判断与解释。
**硬规则**：有 §17 网格时，Agent **禁止手算**，只许「查表选行 + 说明理由」。

---

## 一、快速开始（3 步）

### 前置检查

```powershell
cd D:\Codes\TurtlePi

# 1. 检查 venv
Test-Path skill\.venv\Scripts\python.exe          # 应为 True

# 2. 检查数据源可用性
& skill\.venv\Scripts\python.exe skill\scripts\data_collector.py --list-sources
# 预期: 当前可用: ['zzshare', 'akshare']

# 3. 检查 subagent 是否可用
pi --print "列出你可用的 subagent 相关工具名称"
# 预期: subagent / subagent_supervisor / contact_bg_wait ...
```

### 跑单只股票（A股）

```powershell
$CODE = "600887.SH"
$OUT  = "output\600887_伊利股份"
$PY   = "D:\Codes\TurtlePi\skill\.venv\Scripts\python.exe"

# ── 步骤 1：数据采集（17 段，含 §17 网格）──
& $PY skill\scripts\data_collector.py --code $CODE --output "$OUT\data_pack_market.md"

# ── 步骤 2：年报 PDF（母公司报表 §3P/§4P 需要）──
& $PY skill\scripts\cninfo_downloader.py --code $CODE --year 2024 --save-dir $OUT
# 下载后用 pdf_parent_extractor 自动补 §3P/§4P（data_collector 会自动查找 PDF）

# ── 步骤 3：预算指标 CM§1-5 ──
& $PY skill\scripts\quality_control.py --input "$OUT\data_pack_market.md" --output "$OUT\computed_metrics.md"
```

### 跑 Agent 三段流水线

```
① /business-analysis {code}     → qualitative_report.md（定性 6 维度）
② /turtle-analysis {code}       → phase3_quantitative.md + 最终分析报告
③ /valuation {code}             → 独立估值（可选）
```

或在对话里直接说：

```
用 qualitative agent 分析 600887.SH，数据包在 output/600887_伊利股份/
用 quant agent 分析 600887.SH
用 valuation agent 出最终报告
```

---

## 二、目录结构

```
D:\Codes\TurtlePi\
├── AGENTS.md                     本文件（项目初始化指南）
├── README.md                     移植计划与进度
├── skill/                        pi skill 源（改完后同步到 ~/.pi/agent/skills/）
│   ├── SKILL.md                  入口：流程/公式/硬约束
│   ├── scripts/
│   │   ├── data_collector.py     ★ 统一采集入口（17 段路由）
│   │   ├── derived_metrics_local.py  §17 网格预计算
│   │   ├── quality_control.py    CM§1-5 确定性预算
│   │   ├── cninfo_downloader.py  巨潮年报下载
│   │   ├── pdf_parent_extractor.py 母公司报表提取
│   │   ├── tushare_client.py     Tushare 客户端（库，备用）
│   │   ├── valuation_engine.py   估值引擎
│   │   ├── screener_core.py      选股筛选
│   │   └── datasource/           ★ 数据层（防腐层）
│   │       ├── schema.py         Value + 数据集定义
│   │       ├── base.py           抽象基类 + 注册表
│   │       ├── mapper.py         字段映射
│   │       ├── api.py            get() / compare()
│   │       ├── router.py         逐段路由表
│   │       ├── risk_check.py     §13 风险检测
│   │       └── adapters/         akshare / zzshare / tushare
│   ├── strategies/turtle/        龟龟策略 prompt
│   └── shared/qualitative/       定性模块
├── agents/                       4 个 agent 定义（源，已装到 ~/.pi/agent/agents/）
├── prompts/                      3 个命令模板（源，已装到 ~/.pi/agent/prompts/）
├── notes/                        实测与分析文档（必读）
└── output/                       运行产物（gitignore）
```

---

## 三、数据层（核心）

### 数据层（核心）

```
业务代码 → datasource.get() → Adapter → AkShare / zzshare / Tushare
                    ↑
            切源只改 router.py，业务代码零改动
```

**三层结构**：
1. **schema**：字段名用**中文业务语义**（不是 Tushare 英文名）+ 每个值带 `unit`/`currency`/`period` 元信息
2. **mapper**：双向映射，支持运行期 override
3. **adapters**：每个源一个适配器，异常包装 + 自动降级

### §17 衍生指标网格（九个）

| 子节 | 内容 |
|---|---|
| §17.1 | 财务趋势（CAGR/负债率/净现金） |
| §17.2 | Factor2 输入（C/M/Rf/II/市值） |
| **§17.3** | 真实现金收入（步骤3）：S − AR净增 − 预收净减 + 收款比率 |
| **§17.4** | 经营性现金支出（步骤5）：W1供应商 + W2员工 + W3税款 + W4利息 |
| **§17.5** | 真实可支配现金结余（步骤8）：输出 AA_2y / AA_all / AA_excl |
| §17.6 | 价格分位（基于 §11 周线） |
| **§17.8** | 绝对估值仪表盘：PE/PB/FCF收益率/商誉占比 |
| **§17.9** | 业绩下滑敏感性：基准/-10%/-20%/-30% |
| §17.10 | 支付率 M 三重校验 + Python 内裁定 M_rec |
| §17.11 | 穿透回报率网格（AA变体×税率）+ 目标买入价 |
| §17.12 | G 系数网格（0.7–1.8 × 12 档） |
| §17.13 | λ 收入敏感性 + 临界收入倍数 |

**§17.11 优先用 §17.5 的完整 11 步 AA**（不再回退 OCF−Capex 简化式）。

### 源优先级（按数据集配置）

| 数据段 | 源优先级 |
|---|---|
| §1-7, §9, §11, §12, §14-16 | akshare → zzshare → tushare |
| §3P, §4P 母公司报表 | **cninfo_pdf** → tushare |
| §13 风险警示 | local（确定性检测） |
| §17 衍生指标 | local（网格预计算） |

### 常用 API

```python
import sys; sys.path.insert(0, r"D:\Codes\TurtlePi\skill\scripts")
from datasource import get, compare, crosscheck

# 取数据（auto 按优先级降级）
r = get("利润表", "600887.SH", source="auto")
rec = r[0]
print(rec.get("营业收入"))            # Value(115,393.31 元 CNY)
print(rec.get("营业收入").to_yi())    # 1153.93 亿元
print(rec.get("营业收入").period)     # '20241231'

# 多源对比（发现字段漂移）
print(crosscheck("利润表", "600887.SH", ["akshare", "zzshare"]))
```

### 命令行

```powershell
$PY = "D:\Codes\TurtlePi\skill\.venv\Scripts\python.exe"

# 全量采集（17 段）
& $PY skill\scripts\data_collector.py --code 600887.SH

# 限定段 / 源
& $PY skill\scripts\data_collector.py --code 600887.SH --sections 3,4,5
& $PY skill\scripts\data_collector.py --code 600887.SH --sources akshare

# 跳过 §17
& $PY skill\scripts\data_collector.py --code 600887.SH --no-section17

# 单独做 §17 网格
& $PY skill\scripts\derived_metrics_local.py --input <datapack> --output section17.md
```

---

## 四、Agent 与约束

### 两类 agent（来源不同）

| | **项目 agent** | **内置 agent** |
|---|---|---|
| 提供者 | 本项目（`~/.pi/agent/agents/`） | `pi-subagents` npm 包自带 |
| 面向 | **龟龟投资框架**（知道 §17 / datapack / 框架规范） | **通用软件开发**（代码侦察/审查/实现/研究） |
| 升级 | 自行维护，不受包更新影响 | 随包更新 |
| 同名冲突 | **项目 agent 优先** | — |

**内置 agent 不可能知道** 龟龟框架的概念（`§17.11 网格`、`data_pack_market.md`、`穿透回报率` 等）。

### 什么时候用哪个

| 需求 | 用 |
|---|---|
| 龟龟框架分析（定性/定量/估值/审计） | **项目 agent** |
| 代码审查、代码侦察、联网研究、实现代码 | **内置 agent** |
| 对当前方案提第二意见 | **内置** `oracle` |

内置 agent 清单（11 个）：`scout`（代码侦察）· `researcher`（联网研究）· `evidence-auditor`（来源核查）· `worker`（实现代码）· `reviewer`（代码审查）· `oracle`（第二意见）· `delegate`（通用）· `claude-code`/`codex-exec`/`cursor-agent`（外部 CLI 桥接，含 `*-writer` 变体）

### 4 个项目 agent

| Agent | 职责 | **工具权限** | 输出 |
|---|---|---|---|
| `qualitative` | 6 维度定性（PDF-first + Greenwald 护城河） | read/write/edit | `qualitative_report.md` |
| `auditor` | clean-room 独立重算（防锚定） | **只读**（无 write/edit） | `cleanroom_metrics.md` |
| `quant` | Step 0 校验 + 11 步穿透回报率 | read/write/edit | `phase3_quantitative.md` |
| `valuation` | 估值 + 报告组装（**唯一有权裁决**） | read/write/edit | `{公司}_{代码}_分析报告.md` |

> `auditor` 无写权限是**有意设计** —— 审计员不能改写任何东西，
> 也就无法「顺手改一下让它对上」。

### 如何调用

**方式 1（推荐）：自然语言，pi 自动选 agent**
```
用 qualitative agent 分析 600887.SH，数据包在 output/600887_伊利股份/
用 quant agent 对 600887.SH 做定量分析
用 valuation agent 出最终报告
```

**方式 2：显式调用 subagent 工具**
```
subagent({ agent: "quant", task: "..." })
subagent({ tasks: [ {...}, {...} ] })      ← 并行
subagent({ chain: [ {...}, {...} ] })      ← 串行
```

### 并行时序（重要，不可破坏）

Step 2 **必须同时启动** `qualitative` 与 `auditor`：
```
subagent({ tasks: [
  { agent: "qualitative", task: "..." },
  { agent: "auditor",     task: "..." }
]})
```

**原因**：`auditor` 禁读草稿，此刻草稿尚不存在 → 无从被锚定。
若串行（先 qualitative 再 auditor），防锚定机制失效。

### 硬约束（写在 SKILL.md 和每个 agent prompt 里）

1. **有 §17 网格时禁止手算** —— 只许查表选行 + 说明理由
2. **数字必须带溯源** —— `[src: DP§3]` / `[src: CM§4]` / `[src: 年报P.45]` / `[src: 推断]`
3. **不编造数据** —— 缺失标注 `⚠️ 数据不可用`，禁用「大约/预计」掩盖
4. **金额单位百万元**，千位逗号，百分比 2 位小数
5. **禁止向 `strategies/` `shared/` `scripts/` 写文件**
6. `auditor` **禁读草稿**（`qualitative_report.md` / `phase3_quantitative.md` / `computed_metrics.md`）防锚定

---

## 五、关键公式

```
穿透回报率 = [基准值 × 支付率 M × (1 − 税率 Q) + 回购 O] / 市值

  粗算 R  : 基准值 = C（归母净利润）
  精算 GG : 基准值 = AA（真实可支配现金结余）

门槛 II = A股 max(3.5%, Rf+2%) | 港股 max(5%, Rf+3%) | 美股 max(5%, Rf_US+3%)
安全边际 KK = GG − II              → 仓位矩阵
目标买入价 = 当前股价 × GG / II

Owner Earnings OE = C + D − D×G   （G 从 §17.12 网格选，D = 折旧摊销）
```

---

## 六、环境依赖（重要）

### pi 环境

| 项 | 值 | 备注 |
|---|---|---|
| pi 版本 | 全局 CLI **1.2.0** | 0.85.1 会导致 subagent 子 runner 崩 |
| subagent | **pi-subagents** (npm) | 见 notes/pi-upgrade.md |
| Junction | `~/.pi/agent/npm/node_modules/@earendil-works` → pi-web 的 1.2.0 | **必须存在** |

**若 subagent 报 `toToolDeclaration is not a function` 或 `--port` 错误**：
见 `notes/pi-upgrade.md` / `notes/subagent-windows-fix.md`。

### Python 依赖

```
pandas · tushare · pdfplumber · jinja2 · yfinance · tqdm · pytest · markdown · akshare · zzshare
```

重装：`skill\.venv\Scripts\python.exe -m pip install -r skill\references\requirements.txt`

---

## 六、本项目特有的坑（务必先读）

| # | 坑 | 状态 |
|---|---|---|
| 1 | **AkShare「派息」是每 10 股派 X 元**（非每股） | 已修 |
| 2 | **财报比率不可累加**（毛利率/净利率是单季值，需均值） | 已修 |
| 3 | **东财 `push2*.eastmoney.com` 被 IP 风控** | 已加降级 |
| 4 | **巨潮 orgId 无法用公式推**（须查 API） | 已修 |
| 5 | **母公司报表仅年报 PDF 有**（免费源全无） | 已解决，覆盖 7/7 |
| 6 | **§1 基本信息**依赖三路径降级（东财→雪球→本地推导） | 已修 |
| 7 | **无框线 PDF 表格**需用 `text` 策略 | 已修 |
| 8 | **银行标题用「公司/本行」**而非「母公司」 | 已修 |
| 9 | **PDF 表格把「附注」列当数值**（银行尤甚） | 已修（文本行解析） |
| 10 | **银行报表页被误判为目录页** | 已修 |
| 11 | **投资支付的现金含理财滚动**，不可全额扣除 | 已修（§17.5） |
| 12 | **§17.13 与 §17.11 的 M 窗口不同**（近3年 vs 近5年） | 已知，以 §17.11 为唯一基准 |

### 母公司报表（§3P/§4P）增强说明

`pdf_parent_extractor.py` 支持：
- **两种提取策略**：表格（lines/text）+ **文本行解析**（无框线/列合并）
- **银行专用科目映射**（`BANK_ITEM_MAPS`，吸收存款/发放贷款/存放央行款项…）
- **标题变体**：母公司 / 本行 / 公司
- **跨页续接**：向后扫至多 4 页

实测覆盖：一般工商企业 + 银行 + 无框线报表 = **7/7**

---

## 八、注意事项

### 数据时效性

- AkShare 财报表**已是累计口径**，无需累加
- zzshare 财报表是**单季度值**，必须累加
- `data_collector` 默认只保留**年报**（12-31）

### 财务口径

- **利润口径**：GAAP 归母 vs 扣非归母 vs 主营经营利润 —— Step 0 必须锚定
- **现金口径**：狭义（现金+短投）vs 广义（+理财/定存）
- **跨币种**：所有项必须同一币种（港股报表 HKD / 市值 HKD）

### 禁止事项

- ❌ 使用 yfinance 的 `payoutRatio` / `dividendRate` / `trailingEps`
- ❌ 把 §12 的比率值当年度值直接用（先确认为年报口径）
- ❌ 直接 `import scripts.xxx`（脚本设计为 CLI，`sys.path` 在 `__main__` 层处理）
- ❌ 批量递归删除文件（本机约定）

---

## 九、验证与自检

```powershell
$PY = "D:\Codes\TurtlePi\skill\.venv\Scripts\python.exe"
$OUT = "output\600887_伊利股份"

# 1. 采集是否 17 段全绿
& $PY skill\scripts\data_collector.py --code 600887.SH --output "$OUT\data_pack_market.md"

# 2. CM§1-5 是否产出（无跳过无警告）
& $PY skill\scripts\quality_control.py --input "$OUT\data_pack_market.md" --output "$OUT\computed_metrics.md"

# 3. §17 关键值自检
& $PY -c "import io; c=io.open(r'$OUT\data_pack_market.md',encoding='utf-8').read(); i=c.find('§17.10'); print(c[i:i+300])"
```

**参考基准（600887.SH 伊利）**：
```
§12 毛利率 33.01% / 净利率 8.20%      （CM§3 5年均值）
CM§4 支付率 2024 = 91.29%             （官方 91.30%）
§17.5 AA_2y = 19,816 / AA_all = 16,131
§17.10 M_rec = 75.81%
§17.11 GG = 8.45% (AA_2y★) / II = 3.70%
§17.11 目标买入价 = 64.17 元
§17.8 FCF收益率 6.36% / PE 15.37x / 商誉占比 1.43%
§17.9 -30% 情景仍达标（GG 6.16% > II 3.70%）
最终裁决 = 观察
```

---

## 十、修改后同步

改动 `skill/` 后需同步到 pi：

```powershell
robocopy "D:\Codes\TurtlePi\skill" "C:\Users\Administrator\.pi\agent\skills\turtle-analysis" `
         /E /XD .venv .git /NFL /NDL /NJH /NJS /NP
```

改动本项目 `agents/` `prompts/` 后，同步到 pi：

```powershell
Copy-Item "D:\Codes\TurtlePi\agents\*.md"  "C:\Users\Administrator\.pi\agent\agents\" -Force
Copy-Item "D:\Codes\TurtlePi\prompts\*.md" "C:\Users\Administrator\.pi\agent\prompts\" -Force
```

> 双向同步：项目目录（版本管理）+ `~/.pi/agent/`（pi 实际加载）
> 两者内容应保持一致。

然后 `/reload` 或重启 pi。

---

## 十一、参考文档（notes/）

| 文件 | 内容 |
|---|---|
| `data-source-research.md` | 数据源平替调研（含可信度评级） |
| `datasource-full-test.md` | 全量源实测 + 最终路由表 |
| `datasource-others-test.md` | efinance/adata/mootdx 等排除依据 |
| `datasource-verify.md` | zzshare/AkShare 数值验证 |
| `pdf-batch-verify.md` | 7 股 PDF 提取批量验证 |
| `e2e-verify.md` | 端到端验证报告 |
| `pi-upgrade.md` | **pi 版本升级 + Junction 修复（含三轮诊断对比）** |
| `subagent-windows-fix.md` | subagent 扩展源码修改记录 |
| `path-audit.md` | 路径适配清单 |
| `pi-version-before.txt` | 升级前版本快照（回滚参考） |

---

## 十二、变更历史要点

| 阶段 | 内容 |
|---|---|
| 数据层 | 3 适配器 + 逐段路由 + 多源对比 |
| 采集器 | 17 段（16 数据 + §17 网格） |
| PDF | 母公司报表 7/7（含银行 + 无框线） |
| §17 | 9 个子节全部实现 |
| 基础设施 | pi CLI 1.2.0 + Junction + pi-subagents |
| 端到端 | 600887.SH 完整分析报告已产出（裁决：观察） |

---

*最后更新：§17 补齐 + 银行报表适配（母公司覆盖 7/7）*
