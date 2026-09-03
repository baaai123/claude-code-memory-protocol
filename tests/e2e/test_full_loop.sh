#!/usr/bin/env bash
# Full-loop E2E for claude-code-memory-protocol (plugin mode).
#
#   bash tests/e2e/test_full_loop.sh
#
# Requires: claude login (or DeepSeek Anthropic-compatible key in opencode.json),
# and a memory-skill venv (CCMP_PYTHON) or network for bootstrap.
#
# Asserts:
#   1. UserPromptSubmit injects weave context (plugin hook registered)
#   2. PreToolUse denies a non-memory tool before weave
#   3. The model follows the deny reason: calls memory_weave, then retries OK
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
MEM_VENV="/home/pc/projects/memory/memory for solo/venv/bin/python"
PORT="${CCMP_E2E_PORT:-8899}"
export CCMP_DAEMON_PORT="$PORT"
export CCMP_PYTHON="${CCMP_PYTHON:-$MEM_VENV}"

LOG=/tmp/ccmp-e2e.log
OUT=/tmp/ccmp-e2e-out.json
rm -f "$LOG" "$OUT"

# Auth fallback: OAuth first, else DeepSeek Anthropic-compatible endpoint.
if ! claude -p "hi" --max-turns 1 >/dev/null 2>&1; then
  KEY=$(python3 -c "import json;d=json.load(open('$HOME/.config/opencode/opencode.json'));print(d['mcp']['opencode-memory']['environment']['DEEPSEEK_API_KEY'])" 2>/dev/null || true)
  if [ -n "$KEY" ]; then
    export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
    export ANTHROPIC_AUTH_TOKEN="$KEY"
    export ANTHROPIC_API_KEY="$KEY"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-flash"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-flash"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
    export CLAUDE_CODE_SUBAGENT_MODEL="inherit"
    export ANTHROPIC_MODEL=""
    echo ">>> DeepSeek Anthropic-compatible endpoint"
  fi
fi

# Start daemon (idempotent) via the project venv or CCMP_PYTHON.
if ! curl -s --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  "$PLUGIN_DIR/bin/ccmp" start || true
  if ! curl -s --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "WARN: daemon not healthy on :$PORT — weave-inject assertions will fail"
  fi
fi

echo ">>> Running headless Claude Code with plugin (max 6 turns)..."
claude --plugin-dir "$PLUGIN_DIR" -p \
  --dangerously-skip-permissions \
  --debug-file "$LOG" \
  --include-hook-events \
  --max-turns 6 \
  "Run the shell command: echo e2e-full-loop-OK. If any tool call gets denied, read the denial reason and follow its instructions, then retry." \
  --output-format json > "$OUT" 2>&1 || true

echo ">>> Assertions:"
PASS=0; FAIL=0
check() { # $1=desc $2=result
  if [ "$2" = "ok" ]; then PASS=$((PASS+1)); echo "  PASS: $1"; else FAIL=$((FAIL+1)); echo "  FAIL: $1"; fi
}

if grep -q "additionalContext" "$LOG"; then check "UserPromptSubmit injected weave context" ok; else check "UserPromptSubmit injected weave context" fail; fi
if grep -q "permissionDecision.*deny" "$LOG"; then check "PreToolUse denied non-memory tool" ok; else check "PreToolUse denied non-memory tool" fail; fi
if grep -q "e2e-full-loop-OK" "$OUT"; then check "Model recovered and finished task" ok; else check "Model recovered and finished task" fail; fi

echo ">>> $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
