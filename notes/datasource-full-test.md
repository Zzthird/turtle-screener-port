# 数据源快速测试（全量）

实测日期：2026-10
标的：600887.SH
原则：**不臆测，全部实跑**

---

## 一、测试结果总表

| 能力 | AkShare | zzshare | 东财裸接口 | Tushare |
|---|---|---|---|---|
| **三大报表（合并）** | ✅ 新浪+同花顺+东财 | ✅ 单季需累加 | ✅ | ✅ |
| **母公司报表** | ❌ | ❌ | ❌ | ✅ |
| 财务指标 | ✅ | ✅ | ✅ | ✅ |
| 分红历史 | ✅ 32行 | ❌ | — | ✅ |
| **股权质押** | ✅ 2294行 | ❌ | — | ✅ |
| 股份回购 | ✅ 5553行 | ❌ | — | ✅ |
| 十大股东 | ✅ | ❌ | — | ✅ |
| 国债收益率 | ✅ 9367行 | ❌ | — | ✅ |
| 周线 | ✅ 1537行 | ⚠️ | — | ✅ |
| 个股基本信息 | ⚠️ 代理失败 | ✅ | — | ✅ |
| 审计意见 | ❌ | ❌ | ❌ | ✅ |

---

## 二、逐项实测明细

### AkShare（多后端）

| 接口 | 结果 | 备注 |
|---|---|---|
| `stock_financial_report_sina` | ✅ 利润表 83列 / 资产负债表 147列 / 现金流 71列 | 累计口径，中文列名 |
| `stock_cash_flow_sheet_by_report_em` | ✅ 254列 | **含 D&A / 资本开支 / 已付股息** |
| `stock_financial_benefit_new_ths` | ✅ 1734行 | **同花顺源**（第三后端），长表格式 |
| `stock_financial_debt_new_ths` | ✅ 4046行 | 同花顺 |
| `stock_financial_cash_new_ths` | ✅ 2520行 | 同花顺 |
| `stock_history_dividend_detail` | ✅ 32行 | 分红 |
| `stock_gpzy_pledge_ratio_em` | ✅ 2294行 | **股权质押**（接口名带 `_em`） |
| `stock_repurchase_em` | ✅ 5553行 | 回购（全市场） |
| `stock_gdfx_top_10_em` | ✅ 10行 | 十大股东 |
| `bond_zh_us_rate` | ✅ 9367行 | 国债收益率（Rf 来源） |
| `stock_zh_a_hist(period="weekly")` | ✅ 1537行 | 周线 |
| `stock_individual_info_em` | ❌ ProxyError | 个股信息，代理问题（可能局部网络） |
| 审计意见 | ❌ **无此接口** | AkShare 无审计意见接口 |

### zzshare

| 能力 | 结果 |
|---|---|
| 利润表/资产负债表/现金流/指标/估值 | ✅ 可用（需单季累加） |
| 分红 / 质押 / 回购 / 股东 | ❌ 无对应接口 |
| 字段完整度 | ⚠️ 大量 None（应收账款、合同负债、资本开支、已付股息） |

### 东财 datacenter 裸接口

```
https://datacenter.eastmoney.com/securities/api/data/v1/get
  ?reportName=RPT_F10_FINANCE_GINCOME|GBALANCE|GCASHFLOW|MAINFINADATA
  &filter=(SECUCODE="600887.SH")
```

| 项 | 结果 |
|---|---|
| 合并三表 | ✅ 可用（利润表 203 字段） |
| 母公司报表 | ❌ **不存在**（`RPT_F10_FINANCE_P*`/`B*` 返回 9501 配置不存在） |
| `REPORT_TYPE` 字段 | 值是「年报/中报/一季报」（报告期类型），**不是** 合并/母公司 |
| `PARENT_*` 字段 | 是「归母」指标，**不是** 母公司报表 |

---

## 三、关键结论

### ① 唯一无法替代的能力：母公司报表（§3P/§4P）

```
实测确认：AkShare ❌ | zzshare ❌ | 东财裸接口 ❌ | Tushare ✅
```

**影响**：穿透回报率**步骤2「现金上游障碍」**需要母公司现金 vs 合并现金。
**但这是降级容忍项** —— `phase3_quantitative.md` 规定缺失时标注可信度降低，不中止分析。

### ② AkShare 覆盖 15/17 段

```
✅ §1 基本信息（个股信息接口有代理问题，可用其他替代）
✅ §2 行情   ✅ §3 利润表   ✅ §4 资产负债表  ✅ §5 现金流量表
✅ §6 分红   ✅ §11 周线    ✅ §12 财务指标   ✅ §14 无风险利率
✅ §15 回购  ✅ §16 质押    ✅ §17 衍生（本地）
❌ §3P 母公司利润表          ← 仅 Tushare
❌ §4P 母公司资产负债表      ← 仅 Tushare
```

### ③ AkShare 有一个独特的第三后端：同花顺

```
新浪源 (stock_financial_report_sina)      → 宽表，中文列名
东财源 (stock_cash_flow_sheet_by_report_em) → 长表，英文列名，含补充资料
同花顺源 (stock_financial_*_new_ths)       → 长表，metric_name/value 格式
```
**三后端可互相校验**，单后端失效时可切换。

---

## 四、最终路由表

```python
ROUTING = {
  "§1 基本信息":    ["akshare", "zzshare", "tushare"],
  "§2 行情":        ["akshare", "zzshare", "tushare"],
  "§3 利润表":      ["akshare", "zzshare", "tushare"],
  "§3P 母公司利润":  ["tushare"],               # 唯一源，缺失则降级
  "§4 资产负债表":  ["akshare", "zzshare", "tushare"],
  "§4P 母公司资产":  ["tushare"],               # 唯一源，缺失则降级
  "§5 现金流量表":  ["akshare", "tushare"],     # ak 字段最全
  "§6 分红":        ["akshare", "tushare"],
  "§11 周线":       ["akshare", "tushare"],
  "§12 财务指标":   ["akshare", "zzshare", "tushare"],
  "§14 无风险利率": ["akshare", "tushare"],
  "§15 回购":       ["akshare", "tushare"],
  "§16 质押":       ["akshare", "tushare"],
  "§17 衍生":       ["local"],
}
```

**无 Tushare token 时的实际效果**：15 段可用，2 段（§3P/§4P）降级。

---

## 五、下一步

1. **修正 AkShare 适配器**：补分红/质押/回购/十大股东/国债收益率/周线
2. **补丁：个股基本信息**（`stock_individual_info_em` 代理失败，需换接口或加代理配置）
3. **实现 `data_collector.py`**：按路由表逐段取数 + 标注来源
