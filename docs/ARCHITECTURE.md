# claude-code-memory-protocol — 正式架构方案

**版本**: v1 (2026-09-03)
**状态**: 已批准（基于原型 E2E 验证）
**原型**: `/home/pc/projects/cc-memory-prototype`（原地升级为正式项目）

---

## 1. 目标与定位

为 Claude Code 提供与 [dsh-memory-protocol](https://github.com/baaai123/dsh-memory-protocol) 等价的**强制记忆协议**：桥接 opencode-memory (memory-skill) MCP server，并在工具调用前强制 `memory_weave`、每轮自动注入记忆上下文、会话自动 ingest。

与 dsh 版的架构对照：

| dsh 概念 | Claude Code 等价物 |
|---|---|
| Cordis 插件 bundle | `.claude-plugin/` 插件（经 marketplace 分发） |
| `tools/pre-execute` hook | `PreToolUse` hook（已验证：deny 不可被 bypassPermissions 绕过） |
| `agent/pre-step` hook | `UserPromptSubmit` hook（additionalContext 注入） |
| `agent/turn-stopping` hook | `Stop` / `SubagentStop` hook（读 transcript JSONL 增量 ingest） |
| 进程内 MCP client 桥接 | 常驻 HTTP MCP daemon（多会话共享） |

---

## 2. 进程模型 — 常驻 daemon

### 2.1 问题

memory-skill 的 `mcp_server.py` 当前是**纯 stdio + 无状态**：每个连接新建 `MemorySkill` 实例，embedder ONNX 惰性加载 ~30s。若每个 Claude Code 会话 spawn 一个 stdio 进程：
- 每次冷启动 30s+，且 embedder 模型不共享
- 多会话并发读写同一 sqlite → 命中已知的 sqlite 跨线程缺陷

### 2.2 方案

新增 **常驻 HTTP daemon**（基于 mcp 1.29.0 的 `streamable_http_manager`，已验证可用）：

```
                     ┌─────────────────────────────────────┐
                     │  claude-code-memory-protocol daemon  │
Claude Code session ─┤  (python -m ccmp.daemon --port 8765) │
  UserPromptSubmit ──┤  ├─ MemorySkill (单实例, embedder 常驻)│
  PreToolUse ────────┤  ├─ HTTP/SSE MCP endpoint            │
  Stop ──────────────┤  └─ /weave /ingest /search ...       │
                     └─────────────────────────────────────┘
```

- daemon 是**唯一**持有 MemorySkill + embedder 的进程，启动时预热，之后 weave/search 亚秒级
- 多个 Claude Code 会话通过同一个 HTTP MCP endpoint 共享
- hooks（shell 脚本）通过 HTTP 调 daemon 而非 spawn 新 python 进程

### 2.3 daemon 生命周期

- 启动：`ccmp daemon start`（检测已在运行则跳过）→ 预热 embedder → 监听 127.0.0.1:8765
- hooks 每次调用前 ping `/health`，未运行则自动拉起（lock 文件防竞态）
- 退出：空闲超时或 `ccmp daemon stop`

### 2.4 配置

```yaml
# ~/.claude-code-memory-protocol/config.yml
daemon:
  host: 127.0.0.1
  port: 8765
  idle_timeout_min: 60
memory:
  db_path: ~/.claude-code-memory-protocol/memory.db   # 默认独立库，可指向共享库
  model_dir: ~/models/bge-large-en-v1.5
protocol:
  enforce_weave: true
  inject_weave: true
  auto_ingest: true
  allowlist: []            # 豁免工具名（正则）
  max_context_chars: 8000  # weave 注入截断
  max_ingest_chars: 4000   # ingest 截断
```

---

## 3. 插件结构（交付形态）

```
/home/pc/projects/cc-memory-prototype/      ← 升级为正式项目
├── .claude-plugin/
│   └── plugin.json           # 清单：name/version/description
├── hooks/
│   ├── hooks.json            # PreToolUse + UserPromptSubmit + Stop 声明
│   ├── gate.py               # PreToolUse: 强制 weave gate（已验证）
│   ├── weave_inject.py       # UserPromptSubmit: weave + additionalContext 注入
│   ├── ingest.py             # Stop: 读 transcript JSONL 增量 → ingest
│   └── lib/
│       ├── daemon_client.py  # HTTP 调 daemon（含自动拉起）
│       └── state.py          # 每轮 gate 状态（复用原型逻辑）
├── daemon/
│   ├── __init__.py
│   ├── __main__.py           # ccmp daemon 入口
│   ├── server.py             # streamable_http MCP server 包装
│   └── cli.py                # start/stop/status
├── bin/
│   └── ccmp                  # 可执行入口（daemon 管理 + health check）
├── tests/
│   ├── test_gate.py          # 原型 T1-T5 逻辑测试迁移
│   ├── test_inject.py
│   ├── test_ingest.py
│   └── e2e/
│       └── test_full_loop.sh # 完整闭环 E2E（复用已验证脚本）
├── .mcp.json                 # daemon HTTP endpoint（插件自带）
├── docs/
│   └── ARCHITECTURE.md       # 本文档
└── package.json              # 版本/脚本（npm 仅作附带渠道）
```

### 3.1 plugin.json

```json
{
  "name": "claude-code-memory-protocol",
  "description": "Force memory_weave before every tool call, inject memory context per turn, auto-ingest sessions.",
  "version": "0.1.0",
  "author": { "name": "baaai123" }
}
```

### 3.2 hooks.json（声明三事件）

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [{
          "type": "command",
          "command": "${CLAUDE_PLUGIN_ROOT}/hooks/gate.py",
          "args": []
        }]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [{
          "type": "command",
          "command": "${CLAUDE_PLUGIN_ROOT}/hooks/weave_inject.py",
          "args": []
        }]
      }
    ],
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "${CLAUDE_PLUGIN_ROOT}/hooks/ingest.py",
          "args": []
        }]
      }
    ]
  }
}
```

> 路径规范（官方已确认）：plugin hooks 命令用 **exec form**（`command` + `args` 数组，无 shell 中转，避免转义/注入），脚本绝对路径用 `${CLAUDE_PLUGIN_ROOT}` 占位（安装时解析）；项目相对路径用 `${CLAUDE_PROJECT_DIR}`。python 解释器路径不写死——scripts 首行 `#!/usr/bin/env python3` 由 exec form 直接执行，或 daemon venv 路径经 plugin option 注入。

### 3.3 .mcp.json（MCP daemon）

```json
{
  "mcpServers": {
    "opencode_memory": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

---

## 4. 三个 hook 的职责（对齐 dsh 三 hook）

### 4.1 `PreToolUse` gate（gate.py）— 已验证

- memory 工具（`mcp__opencode_memory__*`）放行；`memory_weave` 调用即置位本轮 gate
- 非 memory 工具：本轮未 weave → 输出 deny JSON（reason 指引调 memory_weave）
- 无 memory 工具时 fail-open（不阻塞 agent）
- deny 在 bypassPermissions 下仍生效（官方保证，E2E 实测）

**新增**: 检查 daemon 存活，未运行自动拉起（fail-open 若拉起失败）。

### 4.2 `UserPromptSubmit` weave + 注入（weave_inject.py）— 新

dsh `agent/pre-step` 等价：
1. 重置本轮 gate（turn_start 逻辑并入）
2. HTTP 调 daemon `/weave`，参数 `user_message=<当前 prompt>`
3. 返回 `hookSpecificOutput.additionalContext` 注入 weave 上下文（含激活引导，一次性）

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "[memory-protocol] <weave 8 块上下文, 截断 8000 chars>"
  }
}
```

**性能关键**: weave 必须走常驻 daemon（HTTP），否则每次用户消息冷启 30s 会让 Claude Code 卡死。daemon 不可达 → fail-open（不注入，不阻塞）。

### 4.3 `Stop` 自动 ingest（ingest.py）— 新

dsh `agent/turn-stopping` 等价。Claude Code hook 拿不到对话历史，改用 **transcript JSONL 增量**：

1. hook 输入含 `session_id` + `transcript_path`（真实格式已确认：`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`）
2. 记录已处理偏移（`~/.claude/state/<session>.offset`）
3. 读取新增行，提取 `user`/`assistant` 消息的文本内容（已实测 JSONL 结构：`type=user/assistant` + `message.content`）
4. HTTP 调 daemon `/ingest`，拼装 `<4000 chars` 的对话块

```json
{"parentUuid":null,"type":"user","message":{"role":"user","content":"..."},"timestamp":"..."}
{"message":{"type":"message","role":"assistant","content":[{"type":"text","text":"..."}]}}
```

**边界**: `Stop` 也在 subagent 结束时触发 → 用 `SubagentStop` 单独处理或靠 offset 去重；主会话 `Stop` 用 transcript offset 保证幂等。

---

## 5. daemon 内部设计

### 5.1 MCP 层

`memory_skill.mcp_server.main()` 当前写死 `stdio_server()`。改造方案：
- daemon 进程 import `memory_skill` 的 `MemorySkill` + `ToolHandler`（非重建 MCP 层）
- 用 `streamable_http_manager` 包一层，暴露同样的 `TOOLS`（34 个 memory_* 工具）

### 5.2 HTTP 快捷端点（hook 用，绕过完整 MCP 握手）

hook 需要的是低延迟单次调用，不值得每 hook 跑完整 MCP initialize 流程。提供精简 REST：

| 端点 | 用途 |
|---|---|
| `GET /health` | daemon 存活 + embedder 就绪 |
| `POST /weave` | `{user_message}` → weave 上下文文本 |
| `POST /ingest` | `{content, role}` → ingest |
| `POST /status` | memory_status 摘要 |

内部复用 `ToolHandler.handle('memory_weave', ...)` 同一套逻辑（不复制业务代码）。

### 5.3 sqlite 并发

- daemon 单进程串行化所有 MemorySkill 调用（asyncio + executor 队列）
- 多会话共享一个 daemon 进程 → 绕开多进程写同一 sqlite 的问题
- 多 daemon 场景（用户手动起了两个）→ sqlite WAL + 启动锁文件防双开

### 5.4 安全性

- 仅监听 127.0.0.1（不暴露局域网）
- 无鉴权（本机信任），若需跨机用 unix socket
- 子进程拉起的 daemon 以调用者身份运行

---

## 6. 安装与分发

### 6.1 本地安装（开发）

```sh
cd /home/pc/projects/cc-memory-prototype
# 1. 建 venv + 依赖
python3 -m venv .venv
.venv/bin/pip install "memory-skill[onnx]" mcp
# 2. 建 config + 启动 daemon
ccmp daemon start
# 3. 本地加载插件
claude --plugin-dir .
# 4. 验证
bash tests/e2e/test_full_loop.sh
```

### 6.2 分发（marketplace）

参照官方 plugin marketplace 流程（已核实）：
```
repo/
├── .claude-plugin/
│   ├── plugin.json
│   └── marketplace.json     # name/owner/plugins 条目
└── (插件本体)
```
用户侧：`/plugin marketplace add baaai123/claude-code-memory-protocol` → `/plugin install claude-code-memory-protocol`。

发布 checklist（镜像 dsh 版经验）：
- [ ] daemon 依赖自动引导（memory-skill pip + 模型下载，参照 dsh bootstrap 脚本，fail-open）
- [ ] 截图 + 双语 README
- [ ] `dsh-plugin` topic 对应物（Claude Code 生态是 marketplace 收录，无独立 topic 需确认）
- [ ] version 语义化，每次发布 bump（官方：用户仅在 version 变更时收到更新）

---

## 7. 里程碑

| 阶段 | 内容 | 验证 |
|---|---|---|
| **M1 daemon 核心** | streamable_http 包装 + /health /weave /ingest + CLI | curl 手测 + 单测 |
| **M2 注入 hook** | UserPromptSubmit weave 注入（gate 逻辑并入） | E2E: prompt 无引导时上下文出现 |
| **M3 ingest hook** | Stop 读 transcript offset 增量 ingest | E2E: 对话出现在记忆库 |
| **M4 插件化** | .claude-plugin 结构 + hooks.json + .mcp.json | `claude --plugin-dir .` + /hooks 可见 |
| **M5 打磨分发** | bootstrap 自动引导、README、marketplace.json、截图 | 干净环境安装测试 |

每个 M 完成后跑 `tests/e2e/test_full_loop.sh`（沿用已验证的教育闭环断言）。

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| daemon 冷启动慢（首次 30s） | daemon 启动即预热 embedder；失败 fail-open 不阻塞 |
| plugin hooks 路径解析坑 | 已确认 `${CLAUDE_PLUGIN_ROOT}` + exec form（args 数组）；M1 前用 `claude --plugin-dir` 实测 |
| Stop hook 与 subagent 的 transcript 边界 | offset 幂等 + SubagentStop 单独匹配 |
| weave 注入超长 / 超时 | max_context_chars 截断 + hook 超时 fail-open |
| memory-skill sqlite 跨线程（已知缺陷） | daemon 单进程串行化 + WAL + 防双开锁 |
| DeepSeek/Anthropic 双后端（用户订阅余额不足） | 端点配置独立于插件；E2E 脚本已支持回退 |
