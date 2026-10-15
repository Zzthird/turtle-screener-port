# 路径审计清单（Step 3）

扫描范围：`D:\Codes\TurtlePi\skill` — 69 文件
扫描时间：Step 3 执行时

---

## 结论：改动量比预期小

`#!` shebang、`.venv/bin` 等只出现在**文档示例**中，**Python 代码本身跨平台良好**（用了 `os.path.join` + `os.path.dirname(__file__)`）。

| 类别 | 位置 | 数量 | 处理 |
|---|---|---|---|
| **A. 文档命令示例** | md 文件 | 中 | 需改（不影响运行，影响可读性） |
| **B. Python shebang** | `*.py` 第 1 行 | 9 处 | **不改**（Windows 无视，无害） |
| **C. Python 硬编码路径** | — | **0 处** | 无需处理 |
| **D. Harness 调用** | md 文件 | 中 | Step 6 处理（本次仅标记） |
| **E. `.env` 加载** | `config.py` | 1 处 | **已合规**（用相对路径） |

---

## A. 文档命令示例（需改）

### A1. `references/README_v2x.md`（541 行，18 处）
```
.venv/bin/python scripts/xxx.py     →  .venv\Scripts\python.exe scripts\xxx.py
bash init.sh                        →  手动 venv 创建（见下）
```
> 该文件是**原仓库 README 存档**，可加一个"Windows 适配"注记段落，不必逐行改。

### A2. Python 脚本 docstring（约 12 处）
```
scripts/download_report.py:9         python3 scripts/xxx.py
scripts/pdf_preprocessor.py:17-19
scripts/quality_control.py:10
scripts/report_to_html.py:5
scripts/screener_core.py:9-11
scripts/tushare_collector.py:11-13
scripts/valuation_engine.py:8
scripts/generate_available_fields.py:8-9
```
> `python3` → `python`（Windows 无 python3），示例路径 `/` → `\`

---

## D. Harness 调用（Step 6 处理，本次仅记录）

### D1. `Task(subagent_type=...)` — 6 处
| 文件 | 行号 | 角色 |
|---|---|---|
| `strategies/turtle/coordinator.md` | 168, 192 | Agent B / Agent C |
| `shared/qualitative/coordinator.md` | 144, 194, 234, 285 | 定性 / PDF附注 / clean-room / 审计 |
| `shared/qualitative/legacy/coordinator_v1.md` | 127-228（5处） | v1 归档，**不处理** |

### D2. `Bash(...)` — 4 处
`shared/qualitative/coordinator.md:169`、`strategies/turtle/coordinator.md:140`、`legacy/coordinator_v1.md:118/153/249`

### D3. `mkdir -p` — 3 处
`shared/qualitative/coordinator.md:90`、`strategies/turtle/coordinator.md:309`、`legacy/coordinator_v1.md:304`

### D4. Claude 专属路径 — 1 处
`strategies/turtle/coordinator.md:21` — `/sessions/*/mnt/uploads/`

### D5. `Skill(...)` 插件调用
`snowball-report-downloader` → 替换为 `scripts/download_report.py`

---

## E. 已合规（无需改）

| 项 | 证据 |
|---|---|
| `.env` 加载 | `config.py`: `os.path.join(os.path.dirname(__file__), "..", ".env")` |
| PDF 查找 | `config.py`: `check_local_pdf(...)` 用 glob |
| 文件读写编码 | 12 处显式 `encoding=` |
| 路径拼接 | 全部 `os.path.join`，无字符串拼 `/` |

---

## Windows 手动建环境（替代 `bash init.sh`）

```powershell
cd D:\Codes\TurtlePi\skill
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\references\requirements.txt
```

> 注意：`requirements.txt` 里若含 numpy/scipy 等，按本机约定应跳过（由 conda 管理）。

---

## 执行计划

1. ✅ 本清单（只扫描）
2. ✅ 改 A2（Python docstring，8 文件 15 行，`python3` → `python`）
3. ✅ 改 A1（README_v2x 顶部加 Windows 适配注记表）
4. ✅ 建虚拟环境 + 装依赖 + CLI 冒烟测试
5. ⏳ D 类留到 Step 6

---

## 执行结果（Step 3 完成）

### 已改
| 文件 | 改动 |
|---|---|
| `scripts/*.py` × 8 | docstring 中 `python3 ` → `python `（15 行） |
| `references/README_v2x.md` | 顶部加 Windows/pi 适配注记表 |

**未改且无需改**：9 处 `#!/usr/bin/env python3` shebang（Windows 无害）、`legacy/` 目录。

### 环境验证

```
Python 3.12.15 (venv @ skill/.venv)
```

安装的依赖：
```
pandas 3.0.6 · tushare 1.4.29 · pdfplumber 0.11.10 · jinja2 3.1.6
yfinance 1.7.0 · tqdm 4.70.1 · pytest 9.1.1 · markdown（补充）
+ 传递依赖：numpy 2.5.4, lxml, cryptography, beautifulsoup4 等
```

### CLI 冒烟测试：8/8 通过

| 脚本 | 结果 |
|---|---|
| `tushare_collector.py` | ✅ usage 正常 |
| `screener_core.py` | ✅ |
| `valuation_engine.py` | ✅ |
| `pdf_preprocessor.py` | ✅ |
| `quality_control.py` | ✅ |
| `report_to_html.py` | ✅（补装 `markdown` 后） |
| `report_consistency.py` | ✅ |
| `download_report.py` | ✅ |

### 发现的问题

1. **`markdown` 包缺失** — 原 `requirements.txt` 漏列，`report_to_html.py` 依赖它。已在 venv 补装，**建议补进 requirements**。
2. **`requirements.txt` 含 `jupyter`/`ipykernel`/`matplotlib`/`pyarrow`** — 本次未装（仅 notebook 用，非核心链路）。
3. **import 方式注意** — 脚本设计为 CLI 运行（`sys.path` 处理在 `__main__` 层），直接 `import scripts.quality_control` 会失败，这是**原设计，非缺陷**。

### 教训 / 后续

- venv 是隔离的，未继承 conda 的 numpy（`--system-site-packages` 默认关）——但 venv 里装了独立的 numpy 2.5.4，与 conda 环境互不干扰。
- 下一步 Step 4/5：写 `SKILL.md` 入口，或先配 `TUSHARE_TOKEN` 跑真实数据。
