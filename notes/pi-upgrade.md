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
