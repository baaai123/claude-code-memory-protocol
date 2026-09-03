# claude-code-memory-protocol

**为 Claude Code 打造的长期记忆插件** — 桥接 [opencode-memory / memory-skill](https://github.com/baaai123/solo-memory) MCP 服务器，并附加强制记忆协议。是 [dsh-memory-protocol](https://github.com/baaai123/dsh-memory-protocol) 的 Claude Code 移植版。

[English](#claude-code-memory-protocol) | [中文](#claude-code-memory-protocol-1)

**让 Claude Code 记住你是谁、你们做过什么** —— 每次工具调用前强制先查阅记忆、每轮对话自动注入跨会话上下文、回合结束自动存档。不再三秒重置、重复学习。

## 作用

插件通过 **3 个 hook + 1 个常驻 daemon** 实现强制记忆协议：

| hook | 对应 dsh hook | 作用 |
|---|---|---|
| `PreToolUse` | `tools/pre-execute` | 未 weave 记忆就调其他工具 → **硬拒绝**（deny 无法被 `--dangerously-skip-permissions` 绕过） |
| `UserPromptSubmit` | `agent/pre-step` | 每轮自动 weave + 注入记忆上下文（additionalContext） |
| `Stop` | `agent/turn-stopping` | 回合结束自动 ingest 对话（transcript 增量，幂等） |

daemon（常驻 HTTP MCP server）持有 memory-skill 实例与嵌入模型，多会话共享——避免每个会话冷启动 30s 加载 ONNX 模型。

## 安装

**前置依赖**：Python ≥3.11（须能以 `python` 调用，如 Windows python.org 安装时勾选 *Add to PATH*；Linux 需 `python-is-python3` 或别名）、Claude Code ≥ 2.1.139（hook `args` 执行体）与网络。首次引导会建 venv、装 memory-skill、可选下载 bge-large-en-v1.5 模型 ~1.3GB（设 `CCMP_SKIP_MODEL=1` 跳过 → SHA-256 降级）。**原生 Windows 支持**：hooks 以 exec 形式直接 spawn `python`，不依赖 Git Bash/cmd 转义；daemon 由 `bin/ccmp`（纯 Python，跨平台）守护，无需 WSL。

### Marketplace 方式（推荐）

```sh
# 1. 添加 marketplace
claude /plugin marketplace add baaai123/claude-code-memory-protocol
# 2. 安装插件（安装摘要若提示 reload，运行 /reload-plugins）
claude /plugin install claude-code-memory-protocol
```

### 本地开发方式

```sh
git clone https://github.com/baaai123/claude-code-memory-protocol
claude --plugin-dir ./claude-code-memory-protocol
```

### 自动引导

首个 hook 触发时若 daemon 不可达，会自动运行 `bin/ccmp-bootstrap`：建 venv → pip 安装 memory-skill/starlette/uvicorn/mcp → 下载模型。全程 fail-open：引导失败不会阻塞 agent，会显示提示。也可手动：

```sh
bin/ccmp start      # 引导 + 启动 daemon（幂等）
bin/ccmp status     # daemon 健康 + 记忆状态
bin/ccmp stop       # 停止 daemon
```

### 环境变量

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CCMP_DAEMON_HOST` / `CCMP_DAEMON_PORT` | `127.0.0.1` / `8000` | daemon 监听地址 |
| `CCMP_PYTHON` | venv → `python` | daemon 解释器 |
| `CCMP_VENV_DIR` | `<plugin>/.venv` | venv 位置 |
| `CCMP_SKIP_BOOTSTRAP` | （未设） | `=1` 关闭自动引导 |
| `CCMP_SKIP_INSTALL` | （未设） | `=1` 跳过 pip install |
| `CCMP_SKIP_MODEL` | （未设） | `=1` 跳过模型下载 |
| `MEMORY_SKILL_DB_PATH` | `~/.claude-code-memory-protocol/memory.db` | 记忆库路径 |
| `HF_ENDPOINT` | `https://huggingface.co` | 模型下载端点（国内用 `https://hf-mirror.com`） |

## 工作原理

```
Claude Code
  ├─ hooks/（PreToolUse · UserPromptSubmit · Stop）─┐  HTTP
  │                                                 ▼
  │                                        ccmp daemon (127.0.0.1:8000)
  │                                     ┌─ REST: /weave /ingest /classify /status  (hook 直调, 免 MCP 握手)
  │                                     ├─ MCP : /mcp  (streamable HTTP, 34 个 memory_* 工具)
  │                                     └─ MemorySkill 单实例 + bge-large-en-v1.5 常驻
  └─ .mcp.json → "opencode_memory" @ http://127.0.0.1:8000/mcp
```

### 强制协议流程

1. 用户发消息 → `UserPromptSubmit` hook：重置本轮 gate + weave 记忆 → 注入上下文
2. 模型调工具 → `PreToolUse` hook：gate 未 weave → **deny**（reason 指引先调 `memory_weave`）
3. 模型调 `mcp__opencode_memory__memory_weave` → gate 打开
4. 回合结束 → `Stop` hook：transcript 增量 + `last_assistant_message` → ingest

## 文档

- [架构方案](docs/ARCHITECTURE.md) — 设计决策、进程模型、里程碑

## 对比 dsh-memory-protocol

| | dsh 版 | Claude Code 版 |
|---|---|---|
| 分发 | npm + DSH plugin | `.claude-plugin/` marketplace |
| hook | `tools/pre-execute` 等 3 个 | `PreToolUse` 等 3 个（机制不同，协议等价） |
| MCP | stdio per-session | 常驻 HTTP daemon（共享 embedder） |
| 自动 ingest | turn-stopping 缓冲 | transcript JSONL 增量 offset |
