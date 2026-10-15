---
description: 龟龟投资策略完整分析（定量 + 估值），需先跑 /business-analysis
argument-hint: "<股票代码>"
---

对股票 **$ARGUMENTS** 执行龟龟投资策略完整分析。

工作目录：`D:\Codes\TurtlePi\skill`
Python：`.venv\Scripts\python.exe`

## 前置检查

1. 确定 `{ts_code}` 与 `{company}`，输出目录 `{output_dir}` = `output/{code}_{company}`
2. **必须存在**（否则停止并提示先跑 `/business-analysis $ARGUMENTS`）：
   - `{output_dir}/qualitative_report.md`
   - `{output_dir}/data_pack_market.md`
3. 可选：`{output_dir}/data_pack_report.md`（缺失则 Agent B 降级）

## Step A：数据采集（16 段路由）

```powershell
.venv\Scripts\python.exe scripts\data_collector.py --code {ts_code} --output {output_dir}/data_pack_market.md
```

无需 Tushare token。源优先级：AkShare → zzshare → Tushare；
§3P/§4P 母公司报表走巨潮 PDF（需先跑 `cninfo_downloader.py`）。

若需刷新市场数据（§1/§2/§11/§14）：
```powershell
.venv\Scripts\python.exe scripts\tushare_collector.py --code {ts_code} --output {output_dir}/data_pack_market.md --refresh-market
```

## Phase 3.1：Agent B（定量）

用 `subagent` 工具调用 `quant` agent：

```
subagent({ agent: "quant", task: "对 {ts_code} 执行定量分析。
  输出目录：{output_dir}
  数据包：{output_dir}/data_pack_market.md（+ data_pack_report.md 若存在）
  写入：{output_dir}/phase3_quantitative.md" })
```

检查 Step 0 裁决：
- `PROCEED` → 继续
- `SUPPLEMENT_NEEDED` → 解析补救请求，WebSearch 补充后重跑（最多 1 次）
- `ABORT` → 通知用户并停止

## Phase 3.2：Agent C（估值 + 报告）

```
subagent({ agent: "valuation", task: "对 {ts_code} 执行估值与报告组装。
  输入：{output_dir}/qualitative_report.md
       {output_dir}/phase3_quantitative.md
       {output_dir}/data_pack_market.md
  写入：{output_dir}/{company}_{code}_分析报告.md" })
```

## 交付前

```powershell
.\.venv\Scripts\python.exe scripts\report_consistency.py --report "{output_dir}/{company}_{code}_分析报告.md"
```

exit 非 0 → 按报告修正数字后重跑。

## 输出

报告：`{output_dir}/{company}_{code}_分析报告.md`

可选转 HTML：
```powershell
.\.venv\Scripts\python.exe scripts\report_to_html.py --input "..." --output "...html"
```
