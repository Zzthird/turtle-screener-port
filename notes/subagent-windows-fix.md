# pi subagent 扩展 Windows 兼容性问题

日期：2026-10 | 状态：**已修复**

---

## 问题现象

```
Agent failed: error: option '-p, --port <port>' argument '--no-session' is invalid.
'--no-session' is not a non-negative number.
```

## 根因

`~/.pi/agent/extensions/subagent/index.ts` 的 `getPiInvocation()`：

```typescript
const execName = path.basename(process.execPath).toLowerCase();
const isGenericRuntime = /^(node|bun)(\.exe)?$/.test(execName);
if (!isGenericRuntime) return { command: process.execPath, args };
return { command: "pi", args };          // ← 问题
```

**Windows 下 `command: "pi"` 会被 Node spawn 解析到 `pi.ps1`**。
但 Node 的 `spawn(cmd, args, {shell: false})` **无法执行 `.ps1`**，
参数被 PowerShell shim 误解析，`-p` 撞上了某个 CLI 的 `--port` 选项。

**验证**：
```
# 直接跑（正常）
pi --mode json -p --no-session "test"          ✅

# 用 pi.cmd（正常）
%APPDATA%\npm\pi.cmd --mode json -p --no-session "test"   ✅
```

## 修复

改 `getPiInvocation()`：Windows 下**优先解析 `pi.cmd`**，找不到才回退到 shell。

```typescript
if (process.platform === "win32") {
    const dir = path.dirname(process.execPath);
    const candidates = [
        path.join(dir, "pi.cmd"),
        path.join(dir, "node_modules", "@agegr", "pi-web", "pi.cmd"),
        path.join(process.env.APPDATA || "", "npm", "pi.cmd"),
    ];
    for (const c of candidates) {
        if (c && fs.existsSync(c)) return { command: c, args };
    }
    return { command: "pi", args, shell: true };   // 最终回退
}
```

同时 `spawn()` 的 `shell: false` 改为跟随 invocation 的 `shell` 标记。

## 影响范围

| 项 | 说明 |
|---|---|
| 影响 | 所有通过 subagent 扩展调用的 agent（本项目的 quant/valuation/qualitative/auditor） |
| 平台 | **仅 Windows**（macOS/Linux 不受影响，`pi` 是可执行文件） |
| 性质 | 扩展自身的跨平台缺陷，**非本项目问题** |

## 修复文件

```
~/.pi/agent/extensions/subagent/index.ts
  - getPiInvocation()  加 Windows .cmd 解析
  - spawn(...)         shell 标记跟随 invocation
```

修复后可正常 `subagent({agent:"quant", task:"..."})`。
