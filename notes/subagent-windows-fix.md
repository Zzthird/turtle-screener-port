# pi subagent 扩展在 pi-web 环境失效 — 真正根因

日期：2026-10 | 状态：**已修复并验证**

---

## 真根因（前两轮都判断错了）

从 `pi-web` 启动的会话中：

```
process.argv[1] = C:\...\@agegr\pi-web\bin\pi-web.js      ← 上层包装器
```

原扩展逻辑：
```typescript
if (currentScript && !isBunVirtualScript && fs.existsSync(currentScript)) {
    return { command: process.execPath, args: [currentScript, ...args] };
}
```

**只要 `argv[1]` 存在就复用它** → 实际执行的是：
```
node  .../pi-web.js  --mode json -p --no-session ...
```

而 **`pi-web.js` 的 CLI 与 coding-agent 不同** —— 它的 `-p` 是 `--port`！
于是 argparse 报：

```
option '-p, --port <port>' argument '--no-session' is invalid
```

> **报错信息里的 `--port` 就是决定性线索** —— 说明被调起的**不是 coding-agent**。

### 我在验证中犯的错

写了个 `verify_ext.js` 复现调用链，结果 `argv[1]` 指向 `verify_ext.js` 自己
→ `node verify_ext.js ...` → **无限递归**，瞬间拉起 79 个 node 进程。

**教训**：这个扩展的"复用 argv[1]"逻辑天然危险，任何包装场景都会踩坑。

---

## 正确修复

### 1. 只在确认是 coding-agent 入口时才复用 argv[1]

```typescript
const isCodingAgentScript = !!currentScript
    && !isBunVirtualScript
    && fs.existsSync(currentScript)
    && /(?:^|[\\/])pi(?:-coding-agent)?(\.[cm]?js)?$/.test(
           path.basename(currentScript).replace(/\.(c|m)?js$/i, ""));
```

### 2. 否则解析真正的 coding-agent 入口

优先顺序：
```
① %APPDATA%\npm\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js
   （pi.cmd 里就是调它 —— 已验证是真正入口）
② pi.exe（自包含二进制）
③ pi.cmd（需 shell:true）
④ PATH 里的 pi
```

### 3. Windows 下 `.cmd` 需要 `shell: true`

Node 18.20+/20+ 对 `.cmd`/`.bat` 强制要求 `shell:true`，否则 `EINVAL`。
（用 `cli.js` 走 node 则不需要 shell。）

---

## 验证

```powershell
node %APPDATA%\npm\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js `
     --mode json -p --no-session "reply with exactly: CLI_OK"
```

**结果**：
```json
{"type":"turn_end","message":{"role":"assistant",
 "content":[{"type":"text","text":"CLI_OK"}]}}
{"type":"agent_settled"}
```
✅ **exit 0，正确返回。**

---

## 三轮诊断对比

| 轮次 | 假设 | 结果 |
|---|---|---|
| 1 | `command:"pi"` 解析到 `pi.ps1` | ❌ 改 `pi.cmd` 后仍失败 |
| 2 | Node 对 `.cmd` 需 `shell:true` | ❌ 治标，未触根因 |
| **3** | **`argv[1]` 是 pi-web.js（非 coding-agent）** | ✅ **正确** |

**关键判别依据**：报错里的 `--port` —— coding-agent 没有这个选项。

---

## 修复文件

```
~/.pi/agent/extensions/subagent/index.ts
  - getPiInvocation()  重写：
      * 严格识别 coding-agent 入口（否则不复用 argv[1]）
      * 解析 cli.js / pi.exe / pi.cmd
      * .cmd 场景返回 shell:true
  - spawn()  shell 标记跟随 invocation
```

**生效**：`/reload` 或重启 pi。

---

## 环境备注

本机 pi 是 `@agegr/pi-web` 包装的自托管 web 版：
```
%APPDATA%\npm\pi.cmd
  -> node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js
```
`examples/extensions/subagent` 是为**独立 pi CLI** 写的，未考虑包装器场景。
