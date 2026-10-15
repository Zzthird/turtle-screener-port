# pi subagent 扩展 Windows 修复（第 2 轮）

日期：2026-10 | 状态：**已修复并验证**

---

## 问题演进

### 第 1 轮（错误诊断）

```
Agent failed: option '-p, --port <port>' argument '--no-session' is invalid
```

**我的初判**：`command: "pi"` 被 Node spawn 解析到 `pi.ps1`，参数错乱。

**尝试的修复**：改用 `pi.cmd` 绝对路径 + `shell: false`。

### 第 2 轮（真正根因）

验证时发现 `.cmd` + `shell:false` 直接报错：

```
Error: spawn EINVAL
    at ChildProcess.spawn (node:internal/child_process:420:11)
```

**真正根因**：**Node.js 18.20+ / 20+ 的安全加固** ——
`spawn()` 对 `.cmd` / `.bat` 文件**要求 `shell: true`**，否则抛 `EINVAL`。
而在 `shell: false` 下回退到 `command: "pi"`，又会命中 PowerShell shim（`pi.ps1`），
参数被 shim 误解析 → 出现 `--port` 的假报错。

---

## 正确修复

```typescript
function getPiInvocation(args: string[]): { command: string; args: string[] } {
    // ... 前面的 execPath 逻辑不变 ...

    // Windows: Node 18.20+/20+ 的 spawn 对 .cmd/.bat 要求 shell:true（否则 EINVAL）
    // 且直接执行 .ps1 会失败，因此统一用 shell + pi 命令名。
    if (process.platform === "win32") {
        return { command: "pi", args, shell: true };
    }
    return { command: "pi", args };
}
```

同时 `spawn()` 的 shell 标记跟随 invocation：

```typescript
const proc = spawn(invocation.command, invocation.args, {
    cwd: cwd ?? defaultCwd,
    shell: (invocation as { shell?: boolean }).shell === true,
    stdio: ["ignore", "pipe", "pipe"],
});
```

---

## 验证

```javascript
// 独立 Node 脚本测试（shell: true）
spawn("pi", ["--mode","json","-p","--no-session","say hi"], { shell: true })
// -> exit: 0 ✅
// -> {"type":"session",...} {"type":"agent_start"} ...
```

---

## 关键理解

| 项 | 说明 |
|---|---|
| `shell: false` + `.cmd` | ❌ Node 18.20+/20+ 抛 EINVAL |
| `shell: false` + `"pi"` | ❌ 解析到 `pi.ps1`，参数错乱（原始报错来源） |
| `shell: true` + `"pi"` | ✅ 由 cmd.exe 正确解析 shim |
| 参数含空格 | ⚠️ 会被 shell 拆分，但 prompt 走 `--append-system-prompt <file>`，不受影响 |

---

## 修复文件

```
~/.pi/agent/extensions/subagent/index.ts
  getPiInvocation()  → Windows 返回 shell: true
  spawn(...)         → shell 标记跟随 invocation
```

**生效方式**：`/reload` 或重启 pi。
