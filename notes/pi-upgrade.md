# pi 全局 CLI 升级记录

日期：2026-10

---

## 背景

`pi-subagents` 的子 runner 崩溃：

```
SyntaxError: The requested module '@earendil-works/pi-ai'
  does not provide an export named 'declarationsEqual'
```

**原因**：两条独立的 pi 安装版本不一致：

| # | 名称 | 升级前 | 位置 |
|---|---|---|---|
| ① | 全局 pi CLI | **0.85.1** | `%APPDATA%\npm\node_modules\@earendil-works\` |
| ② | pi-web 内嵌 | 1.2.0 | `D:\npm\...\@agegr\pi-web\node_modules\@earendil-works\` |

pi-subagents 的子 runner 会把 `pi-ai` 别名到**宿主自带的副本**（即 ①，0.85.1），
而 0.85.1 缺 `declarationsEqual` / `getCurrentSystemMessage` / `toToolDeclaration` 三个符号。

---

## 操作

```powershell
npm i -g @earendil-works/pi-coding-agent@latest
# 0.85.1 -> 1.2.0（removed 21, changed 110 packages）
```

## 验证

| 检查 | 结果 |
|---|---|
| `pi --version` | **1.2.0** ✅ |
| 全局嵌套 pi-ai | **1.2.0** ✅ |
| `declarationsEqual` | function ✅ |
| `getCurrentSystemMessage` | function ✅ |
| `toToolDeclaration` | function ✅ |
| `pi --print "UPGRADE_OK"` | UPGRADE_OK ✅ |
| **subagent 子 runner 实调** | **21（=3×7）✅** |

---

## 同时还需要的：Junction

仅升级 CLI 还不够 —— **父进程（pi-web）找不到 pi-ai**，需 Junction：

```powershell
cmd /c mklink /J "C:\Users\Administrator\.pi\agent\npm\node_modules\@earendil-works" `
                "D:\npm\node_modules\@agegr\pi-web\node_modules\@earendil-works"
```

**两者配合才完整**：
```
Junction  → 修父进程的模块解析
升级 CLI  → 修子 runner 的别名目标
```

---

## 升级后暴露的 2 个既有问题（与升级无关）

### ① pi-mcp-adapter 与内置 mcp 冲突

```
Warning: Extension ...pi-mcp-adapter\index.ts registers command `/mcp`,
so built-in extension `mcp` was not loaded.
```

**建议**：二选一 —— 要么禁内置 mcp，要么移除 pi-mcp-adapter。

### ② pi-web-access 的 typebox 依赖声明错误

```
Warning: Host-provided extension packages must be declared in
peerDependencies with a "*" range, not dependencies: typebox.
```

**建议**：等上游修（属包本身问题）。

---

## 回滚方式

```powershell
npm i -g @earendil-works/pi-coding-agent@0.85.1
```

**注**：pi-web 不受影响（它用自己内嵌的 1.2.0）。

---

## 附：三轮诊断对比（避免重复踩坑）

| 轮次 | 假设 | 结果 |
|---|---|---|
| 1 | `command:"pi"` 被解析到 `pi.ps1` | ❌ 改 `pi.cmd` 后仍失败 |
| 2 | Node 对 `.cmd` 需 `shell:true` | ❌ 治标，未触根因 |
| **3** | **模块解析失败 + 子 runner 别名到旧版** | ✅ 正确 |

**关键判别依据**：
- 报错里的 `--port` → 说明被调起的不是 coding-agent（是 pi-web.js）
- `toToolDeclaration is not a function` → 模块解析失败或版本不符

**衍生教训**：
- `pi --version` 返回的是 **pi-web 包装器版本**（0.12.1），
  与 `@earendil-works/*` 各包版本（1.2.0）是不同体系，勿混淆
- 验证脚本若用 `process.argv[1]` 复用自身，会形成**无限递归**
  （曾瞬间拉起 79 个 node 进程）

---

## 附：曾尝试但失败的方案

**`pi install npm:pi-subagents`**（官方 npm 包）

安装成功（v0.78.0），但加载失败：
```
Error: Failed to load extension pi-subagents\index.js:
  (0, _piAi.toToolDeclaration) is not a function
```

**真因**：其 `src/extension/declaration-pinning.js` 静态 import
`@earendil-works/pi-ai`，但该包在 `~/.pi/agent/npm/node_modules/`
下不可达（pi-ai 被埋在 pi-web 嵌套目录）。

**最终解法**：建 Junction（见上文）+ 升级全局 CLI。
两者配合后 `pi-subagents` 完全可用。
