---
description: 独立估值分析 — 自动分类 + 多方法估值（DCF/DDM/可比/Graham）
argument-hint: "<股票代码>"
---

对股票 **$ARGUMENTS** 执行独立估值分析。

工作目录：`D:\Codes\TurtlePi\skill`
Python：`.venv\Scripts\python.exe`

## 前置条件

需先跑 `/business-analysis $ARGUMENTS`，产出：
- `output/{code}_{company}/qualitative_report.md`
- `output/{code}_{company}/data_pack_market.md`

缺失则停止并提示。

## 执行

1. 读 `strategies/valuation/coordinator.md` 完整流程
2. 读 `strategies/valuation/phase2_valuation.md` 指令
3. 参考：
   - `strategies/valuation/references/valuation_methods.md`（方法论）
   - `strategies/valuation/references/classification_rules.md`（分类规则）
   - `strategies/valuation/references/report_template.md`（报告模板）

## 估值引擎

```powershell
.\.venv\Scripts\python.exe scripts\valuation_engine.py --code {code} --output-dir "output/{code}_{company}/"
```

引擎自动分类并选择方法：DCF / DDM / 可比估值 / Graham。

## 输出

`output/{code}_{company}/{company}_{code}_估值报告.md`

## 硬约束

- 金额单位百万元，千位逗号，百分比 2 位小数
- 数字必须带溯源标注 `[src: ...]`
- 不编造数据，缺失标注 `⚠️ 数据不可用`
- 禁止向 `strategies/` `shared/` `scripts/` 写入文件
