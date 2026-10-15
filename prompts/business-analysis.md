---
description: 商业分析 — PDF-first 六维度定性评估（含 Greenwald 护城河框架）
argument-hint: "<股票代码>"
---

对股票 **$ARGUMENTS** 执行定性商业分析。

工作目录：`D:\Codes\TurtlePi\skill`
Python：`.venv\Scripts\python.exe`

## Step 1：数据采集

**1A — 数据采集（16 段路由）**
```powershell
New-Item -ItemType Directory -Force "{output_dir}"
.venv\Scripts\python.exe scripts\data_collector.py --code {ts_code} --output {output_dir}/data_pack_market.md
```
> 无需 Tushare token（AkShare/zzshare/巨潮 PDF 组合）

**1A2 — 确定性预算指标**
```powershell
.venv\Scripts\python.exe scripts\quality_control.py --input {output_dir}/data_pack_market.md --output {output_dir}/computed_metrics.md
```

**1B — PDF 获取（母公司报表 + WebSearch 补充需要）**
```powershell
.venv\Scripts\python.exe scripts\cninfo_downloader.py --code {ts_code} --year 2024
```
- 用户已提供 PDF → 直接用
- 下载失败 → WebSearch 降级

**1C — PDF 附注提取**
- 纯文本 PDF → 直接 read 关键章节
- 扫描件 → `.\.venv\Scripts\python.exe scripts\pdf_preprocessor.py --pdf <file> --output {output_dir}/pdf_sections.json`
- 输出 `{output_dir}/data_pack_report.md`

参考 `strategies/turtle/phase2_PDF解析.md` 的提取格式规范。

## Step 2：定性分析（并行）

用 `subagent` 工具并行启动：

```
subagent({ tasks: [
  { agent: "qualitative", task: "对 {ts_code} 执行 6 维度定性分析。
      数据源：{output_dir}/ 下的年报 PDF、data_pack_market.md、computed_metrics.md
      写入：{output_dir}/qualitative_report.md" },
  { agent: "auditor", task: "对 {ts_code} 执行 clean-room 独立重算。
      ⚠️ 只读 data_pack，禁止读 qualitative_report.md 和 computed_metrics.md
      写入：{output_dir}/cleanroom_metrics.md" }
]})
```

> **关键**：两者**同时启动**。草稿此刻尚不存在，auditor 无从被锚定。

## Step 3：审计（串行）

**3A 一致性扫描**
```powershell
.\.venv\Scripts\python.exe scripts\report_consistency.py --report {output_dir}/qualitative_report.md
```

**3B 数字审计** — 读 `shared/qualitative/agents/numeric_audit.md`，
对比 `qualitative_report.md` 与 `cleanroom_metrics.md` 的数字，输出 `{output_dir}/audit.md`。
`AUDIT_RESULT: PASS | FIX_REQUIRED`

**3C 修复环**（max 1 次）：按"原文 → 修正"对 Edit 草稿 → 重跑 3A。
critical 未修复 → 禁止交付。

## Step 4：HTML（可选）

```powershell
.\.venv\Scripts\python.exe scripts\report_to_html.py --input {output_dir}/qualitative_report.md --output {output_dir}/qualitative_report.html --standalone
```

## 输出

- `{output_dir}/qualitative_report.md` ← 主交付物（供 `/turtle-analysis` 消费）
- `{output_dir}/data_pack_market.md`
- `{output_dir}/computed_metrics.md`
- `{output_dir}/data_pack_report.md`（若有 PDF）
