# pi-subagents 替换尝试 — 结果：不兼容

日期：2026-10 | 结论：**放弃，回退到修好的官方 example**

---

## 尝试过程

```powershell
pi install npm:pi-subagents      # 安装成功（v0.78.0）
pi --print "/subagents-doctor"
```

**失败**：
```
Error: Failed to load extension "...\pi-subagents\index.js":
  (0, _piAi.toToolDeclaration) is not a function
```

---

## 根因：API 破坏性变更

### 版本核对

| 组件 | 版本 |
|---|---|
| pi-web（CLI 报的版本） | 0.12.1 |
| pi-coding-agent | 1.2.0 |
| **pi-ai** | **1.2.0** |
| pi-subagents | 0.78.0 |

pi-subagents 的 peerDependencies 要求 `pi-ai >= 0.86.1` —— **数值上满足**。

### 实际问题

`toToolDeclaration` 在 pi-ai **1.2.0 的 `transcript` 内部模块中存在**：

```
dist/transcript.d.ts -> export function toToolDeclaration
```

但**未从主入口 re-export**：

```javascript
// 主入口 (dist/index.js) 的导出
含 tool 的: fauxToolCall, getCurrentTools, getDeclaredTools, ...
toToolDeclaration: undefined     ← 不在主入口
```

pi-subagents 按旧版 API 从主入口导入 → 拿到 `undefined` → 调用时崩。

**结论**：
```
pi-subagents@0.78.0  ← 按 pi-ai <=0.86.x 的导出面编写
本机 pi-ai 1.2.0     ← 该函数改为内部模块，不再 re-export
                      → 破坏性变更
```

版本号看似满足，实际 API 面已变。

---

## 处置

```powershell
pi uninstall npm:pi-subagents          # 已卸载
# 恢复修复后的官方 example 扩展
Copy-Item notes\_backup_subagent_extension\* ~/.pi/agent/extensions/subagent\ -Recurse
```

---

## 对比记录

| 项 | 官方 example（现用） | pi-subagents |
|---|---|---|
| 可加载 | ⚠️ 需修复（已完成） | ❌ API 不兼容 |
| 修复成本 | 已投入（3 轮） | 需要改包源码 |
| 功能 | 基础（foreground only） | 丰富（后台/并行/Fleet/doctor） |
| 结论 | **继续用** | 待上游适配 pi-ai 1.2.x |

---

## 后续建议

1. **短期**：继续用修复后的 example 扩展（已验证可用）
2. **中期**：关注 pi-subagents 是否发布适配 pi-ai 1.2.x 的版本
3. **判断依据**：等其主入口重新 re-export `toToolDeclaration`，或改用内部路径导入

> 注：`pi --version` 返回 0.85.1 是 **pi-web 包装器的版本号**，
> 与 `@earendil-works/*` 各包的版本（1.2.0）不同体系，勿混淆。
