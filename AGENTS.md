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
| 数据源 | **AkShare + zzshare + 巨潮 PDF**（A股无需 Tushare token） |

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

### 设计

```
业务代码 → datasource.get() → Adapter → AkShare / zzshare / Tushare
                    ↑
            切源只改 router.py，业务代码零改动
```

**三层结构**：
1. **schema**：字段名用**中文业务语义**（不是 Tushare 英文名）+ 每个值带 `unit`/`currency`/`period` 元信息
2. **mapper**：双向映射，支持运行期 override
3. **adapters**：每个源一个适配器，异常包装 + 自动降级

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

### 4 个自定义 agent

| Agent | 职责 | 输出 |
|---|---|---|
| `qualitative` | 6 维度定性（PDF-first + Greenwald 护城河） | `qualitative_report.md` |
| `quant` | Step 0 校验 + 11 步穿透回报率 | `phase3_quantitative.md` |
| `valuation` | 估值 + 报告组装（**唯一有权裁决**） | `{公司}_{代码}_分析报告.md` |
| `auditor` | clean-room 独立重算（防锚定） | `cleanroom_metrics.md` |

### 硬约束（写在 SKILL.md 和每个 agent prompt 里）

1. **有 §17 网格时禁止手算** —— 只许查表选行 + 说明理由
2. **数字必须带溯源** —— `[src: DP§3]` / `[src: CM§4]` / `[src: 年报P.45]` / `[src: 推断]`
3. **不编造数据** —— 缺失标注 `⚠️ 数据不可用`，禁用「大约/预计」掩盖
4. **金额单位百万元**，千位逗号，百分比 2 位小数
5. **禁止向 `strategies/` `shared/` `scripts/` 写文件**
6. `auditor` **禁读草稿**（`qualitative_report.md` / `computed_metrics.md`）防锚定

### 并行编排（推荐）

```
Step 2 并行启动:
  qualitative  →  qualitative_report.md
  auditor      →  cleanroom_metrics.md     ← 同时启动，草稿尚不存在 → 无从锚定
```

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

## 七、本项目特有的坑（务必先读）

| # | 坑 | 详见 |
|---|---|---|
| 1 | **AkShare「派息」是每 10 股派 X 元**（非每股） | 已修，见 data_collector `_aggregate_dividends` |
| 2 | **财报比率不可累加**（毛利率/净利率是单季值，需均值） | 已修，见 akshare_adapter `_fetch_fina_indicator` |
| 3 | **东财 `push2*.eastmoney.com` 被 IP 风控** | 已加降级，见 notes/datasource-others-test.md |
| 4 | **巨潮 orgId 无法用公式推**（须查 API） | 已修，见 cninfo_downloader `resolve_org_id` |
| 5 | **母公司报表仅年报 PDF 有**（免费源全无） | 见 notes/pdf-batch-verify.md，覆盖 5/7 |
| 6 | **§1 基本信息**依赖三路径降级（东财→雪球→本地推导） | 已修，见 akshare_adapter `_fetch_stock_basic` |
| 7 | **无框线 PDF 表格**需用 `text` 策略 | 见 pdf_parent_extractor `_extract_tables` |

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
§17.10 M_rec = 75.81%
§17.11 GG = 6.20% (AA_2y★) / II = 3.70%
§17.11 目标买入价 = 47.07 元
最终裁决 = 观察
```

---

## 十、修改后同步

改动 `skill/` 后需同步到 pi：

```powershell
robocopy "D:\Codes\TurtlePi\skill" "C:\Users\Administrator\.pi\agent\skills\turtle-analysis" `
         /E /XD .venv .git /NFL /NDL /NJH /NJS /NP
```

改动 `agents/` `prompts/` 后：

```powershell
Copy-Item "D:\Codes\TurtlePi\agents\*.md"  "C:\Users\Administrator\.pi\agent\agents\" -Force
Copy-Item "D:\Codes\TurtlePi\prompts\*.md" "C:\Users\Administrator\.pi\agent\prompts\" -Force
```

然后 `/reload` 或重启 pi。

---

## 十一、参考文档（notes/）

| 文件 | 内容 |
|---|---|
| `data-source-research.md` | 数据源平替调研（含可信度评级） |
| `datasource-full-test.md` | 全量源实测 + 最终路由表 |
| `datasource-others-test.md` | efinance/adata/mootdx 等排除依据 |
| `datasource-verify.md` | zzshare/AkShare 数值验证 |
| `pdf-batch-verify.md` | 7 股 PDF 提取批量验证（5/7） |
| `e2e-verify.md` / `e2e-verify-2.md` | 端到端验证报告 |
| `pi-upgrade.md` | pi 版本升级 + Junction 修复 |
| `subagent-windows-fix.md` | subagent 三轮诊断对比 |
| `path-audit.md` | 路径适配清单 |

---

*最后更新：端到端流程跑通（600887.SH 完整分析报告已产出）*
