#!/usr/bin/env bash
# polygate curl 示例
set -euo pipefail

BASE="${POLYGATE_BASE:-http://127.0.0.1:8000}"
KEY="${POLYGATE_KEY:-change-me}"

echo "== 健康检查 =="
curl -s "$BASE/healthz"
echo ""

echo "== 模型列表 =="
curl -s -H "Authorization: Bearer $KEY" "$BASE/v1/models"
echo ""

echo "== 对话 =="
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}' \
  "$BASE/v1/chat/completions"
echo ""

echo "== 流式对话 =="
curl -s -N -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}],"stream":true}' \
  "$BASE/v1/chat/completions"
echo ""
