#!/usr/bin/env bash
# ホストに直接入れた Ollama を、コンテナのアプリから使う設定にする。
#
#   bash scripts/use-host-ollama.sh
#
# ollama コンテナは起動しない（ポート衝突も、二重のモデル取得も起きない）。
set -uo pipefail
cd "$(dirname "$0")/.."

ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng(){ printf '  \033[31m✗\033[0m %s\n' "$1"; }

[[ -f .env ]] || cp .env.example .env
set_env() {
  if grep -qE "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

PORT=$(sed -n 's/^OLLAMA_PORT=//p' .env | head -1); PORT="${PORT:-11434}"

printf '\n\033[1m== ホストの Ollama を使う\033[0m\n'
if curl -sf -m 3 "http://localhost:${PORT}/api/tags" >/dev/null 2>&1; then
  n=$(curl -sf -m 3 "http://localhost:${PORT}/api/tags" | grep -o '"name"' | wc -l | tr -d ' ')
  ok "ホストの Ollama に到達（モデル ${n} 件）"
else
  ng "ホストの localhost:${PORT} に Ollama が見つかりません"
  echo "      ollama serve  を起動してから、もう一度実行してください"
  exit 1
fi

# コンテナからホストを見る名前。Linux では compose の extra_hosts で解決する
set_env COMPOSE_PROFILES ""
set_env OLLAMA_BASE_URL "http://host.docker.internal:${PORT}"
set_env HYPO_PROVIDER ollama
set_env BIOMNI_SOURCE Ollama
ok "COMPOSE_PROFILES=（空） → ollama コンテナは起動しません"
ok "OLLAMA_BASE_URL=http://host.docker.internal:${PORT}"

printf '\n  反映する:  make docker-rebuild\n\n'
