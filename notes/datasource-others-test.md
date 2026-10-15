# 其余候选源实测（第二轮）

实测日期：2026-10
标的：600887.SH
环境：Windows，系统注册表配置了 `127.0.0.1:7897` 代理（未运行）

---

## 一、测试结果

| 源 | 安装 | 实测结果 | 结论 |
|---|---|---|---|
| **adata** | ✅ | ✅ 全市场代码 5582 行<br>⚠️ 行情 rows=0（需查参数） | 部分可用 |
| **efinance** | ✅ | ❌ ProxyError / ConnectionError | **不可用**（东财 push2 被封） |
| **mootdx** | ✅ | ✅ 连接成功<br>⚠️ bars rows=0 | 部分可用 |
| **pytdx** | ✅ | ✅ 可导入 | 未深测 |
| **Ashare** | ❌ | **PyPI 无此包** | 需从 GitHub 直接下载 |
| **pywencai** | ✅ | ❌ `'NoneType' object has no attribute 'get'` | 需登录态 |

---

## 二、关键根因：东财 push2 域名被 IP 级封禁 ⚠️

### 诊断证据

```
TLS 握手      push2his.eastmoney.com   ✅ TLSv1.3 成功
HTTP 请求     push2his.eastmoney.com   ❌ RemoteDisconnected（服务端立即断开）
              push2.eastmoney.com      ❌ 同样
              datacenter-web.eastmoney.com  ✅ 200
              datacenter.eastmoney.com      ✅ 200
              新浪 hq.sinajs.cn             ✅ 200
```

### 已排除的原因

| 假设 | 验证 | 结论 |
|---|---|---|
| 代理问题 | 清空 env + `trust_env=False` | ❌ 不是原因 |
| DNS 问题 | 解析正常（47.112.165.11） | ❌ |
| TLS 问题 | 握手成功 | ❌ |
| UA/Referer 反爬 | 完整浏览器头 + Chrome 指纹 | ❌ 绕不过 |
| 备用域名 | `88.push2his` / `push2delay` | ❌ 同样被拒 |

**结论：这是**服务器端按 IP 段的风控**，无法从客户端绕过。**

---

## 三、影响范围

### 被 push2 封禁影响的源

| 源/接口 | 依赖域名 | 状态 |
|---|---|---|
| **efinance**（全部） | push2 / push2his | ❌ 完全不可用 |
| AkShare `stock_individual_info_em` | push2 | ❌ |
| AkShare `stock_zh_a_hist` | push2his | ⚠️ 之前成功过（**间歇性**） |
| AkShare 东财系其他接口 | push2* | ⚠️ 不稳定 |

### 不受影响的

| 源/域名 | 状态 |
|---|---|
| `datacenter-web.eastmoney.com`（东财 F10） | ✅ 正常 |
| `datacenter.eastmoney.com` | ✅ |
| `hq.sinajs.cn`（新浪） | ✅ |
| AkShare 新浪系（`stock_financial_report_sina`） | ✅ |
| AkShare 同花顺系（`stock_financial_*_new_ths`） | ✅ |
| zzshare | ✅（自有 API） |

> **注**：AkShare 东财系接口是**间歇性**的——之前 `stock_zh_a_hist` 和 `stock_cash_flow_sheet_by_report_em` 成功，现在失败。说明风控是**动态/限流式**的，不是永久封禁。

---

## 四、修正后的选择

| 源 | 是否推荐 | 理由 |
|---|---|---|
| **AkShare 新浪源** | ✅ **主力** | 稳定，已在第一轮验证数值准确 |
| **AkShare 同花顺源** | ✅ 备用 | 独立第三后端，可交叉校验 |
| **东财 datacenter（F10）** | ✅ 可用 | 域名未被封 |
| **zzshare** | ✅ 交叉验证 | 自有 API，不受东财影响 |
| adata | ⚠️ 待查 | 行情返回空，需确认参数 |
| mootdx/pytdx | ⚠️ 待查 | 返回空，通达信协议可能被防火墙挡 |
| **efinance** | ❌ **排除** | 完全依赖 push2 |
| Ashare | ❌ 排除 | PyPI 无包 |
| pywencai | ❌ 排除 | 需登录态，有封禁风险 |

---

## 五、对架构的启示

**这次实测暴露了一个重要设计点**：

```
东财 push2 封禁 → efinance 全废 + AkShare 部分接口间歇失败
                 ↓
        单一数据源 = 单点故障
                 ↓
     必须「多源 + 自动降级」（数据层已实现）
```

**印证了建 `datasource` 分层的必要性**——如果直接绑定任一单一源，今天就会全线瘫痪。

**建议**：
1. AkShare 优先走**新浪源**（稳定），东财源作为可选加速
2. 适配器需**识别 push2 失败并自动降级**到新浪/同花顺
3. 保留 zzshare 作独立验证源（不受东财影响）

---

## 六、待办

- [x] adata 行情 rows=0 —— **已查清：adata 无财务数据接口**（仅行情/资金流/概念），对框架无价值
- [x] mootdx bars rows=0 —— 已查：`bestip=False` 仍返回空，通达信服务器不可达（防火墙/端口 7709 受限）
- [ ] AkShare 东财系接口加**自动降级**（push2 失败 → 新浪/同花顺）
- [ ] Ashare 从 GitHub 直取（如需要行情）

---

## 七、补充结论（adata / mootdx）

### adata

```
adata.stock.market.*  → 只有行情 / 资金流 / 概念板块 / 百度指数
adata.stock.info.*    → 只有代码列表
无 财务/报表/基本面 接口
```

**结论：对龟龟框架无价值**（框架核心是三大报表）。仅可作为行情备用源。

### mootdx / pytdx

```
mootdx: 可连接，但 bars() 返回空
Reader: 可用（读本地通达信数据文件）
```

**结论：通达信服务器在此环境不可达**（TCP 7709 可能被防火墙/网络策略拦），已被 push2 之后又一个环境限制。若需使用需本地装通达信客户端 + Reader 读本地文件。
