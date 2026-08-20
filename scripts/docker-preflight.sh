#!/usr/bin/env bash
# docker compose up の前に、ポートの衝突と設定の食い違いを見る。
#
#   bash scripts/docker-preflight.sh
#
# 「ports are not available: ... address already in use」を、
# 起動を試みる前に、原因つきで出すためのもの。
set -uo pipefail
cd "$(dirname "$0")/.."

ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

env_of() { sed -n "s/^$1=//p" .env 2>/dev/null | head -1; }

APP_PORT=$(env_of APP_PORT);       APP_PORT="${APP_PORT:-8000}"
OLLAMA_PORT=$(env_of OLLAMA_PORT); OLLAMA_PORT="${OLLAMA_PORT:-11434}"
PROFILES=$(env_of COMPOSE_PROFILES)
PROVIDER=$(env_of HYPO_PROVIDER);  PROVIDER="${PROVIDER:-ollama}"
OLLAMA_URL=$(env_of OLLAMA_BASE_URL)

# そのポートを誰が使っているか
who_has() {
  local port="$1" out=""
  if command -v ss >/dev/null 2>&1; then
    out=$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p"$" {print $NF}' | head -1)
  fi
  if [[ -z "$out" ]] && command -v lsof >/dev/null 2>&1; then
    out=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" (pid "$2")"}')
  fi
  printf '%s' "$out"
}
in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk -v p=":$port" '$4 ~ p"$" {found=1} END{exit !found}' && return 0
  fi
  command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  return 1
}
# その名前のコンテナが自分のものか（= compose が再利用するので衝突しない）
ours() { docker compose ps --format '{{.Service}}' 2>/dev/null | grep -qx "$1"; }

printf '\n\033[1m== 起動前チェック\033[0m\n'
printf '  provider=%s / COMPOSE_PROFILES=%s / APP_PORT=%s / OLLAMA_PORT=%s\n' \
       "$PROVIDER" "${PROFILES:-（空）}" "$APP_PORT" "$OLLAMA_PORT"

FAIL=0

# --- アプリのポート ---
if in_use "$APP_PORT" && ! ours app; then
  ng "APP_PORT=$APP_PORT は既に使われています： $(who_has "$APP_PORT")"
  echo "      .env の APP_PORT を空いている番号に変えてください（例: APP_PORT=8003）"
  FAIL=1
else
  ok "APP_PORT=$APP_PORT は使えます"
fi

# --- Ollama のポート（ollama プロファイルが有効なときだけ関係する）---
if [[ ",$PROFILES," == *",ollama,"* ]]; then
  if in_use "$OLLAMA_PORT" && ! ours ollama; then
    ng "OLLAMA_PORT=$OLLAMA_PORT は既に使われています： $(who_has "$OLLAMA_PORT")"
    cat <<MSG
      ホストに直接入れた Ollama が動いている可能性があります。3 つのどれかを選んでください。

      (A) ホストの Ollama をそのまま使う（コンテナは立てない・おすすめ）
            bash scripts/use-host-ollama.sh

      (B) ホストの Ollama を止めて、コンテナ版を使う
            sudo systemctl stop ollama     # または起動中のプロセスを終了
            make docker-up

      (C) コンテナ版を別ポートで動かす
            echo "OLLAMA_PORT=11435" >> .env && make docker-up
MSG
    FAIL=1
  else
    ok "OLLAMA_PORT=$OLLAMA_PORT は使えます"
  fi
else
  ok "ollama コンテナは起動しません（COMPOSE_PROFILES に ollama が無い）"
  if [[ "$PROVIDER" == ollama ]]; then
    if [[ "$OLLAMA_URL" == *host.docker.internal* ]]; then
      ok "ホストの Ollama を使う設定です（$OLLAMA_URL）"
    else
      warn "provider=ollama なのに ollama コンテナを起動しません"
      echo "      ホストの Ollama を使うなら:  bash scripts/use-host-ollama.sh"
      echo "      コンテナ版を使うなら:        echo 'COMPOSE_PROFILES=ollama' >> .env"
    fi
  fi
fi

# --- Claude API ---
if [[ "$PROVIDER" == anthropic ]]; then
  if grep -qE '^ANTHROPIC_API_KEY=sk-' .env 2>/dev/null; then
    ok "ANTHROPIC_API_KEY 設定済み"
  else
    ng "provider=anthropic ですが ANTHROPIC_API_KEY が未設定です"
    echo "      bash scripts/set-provider.sh claude --key sk-ant-..."
    FAIL=1
  fi
fi

if [[ $FAIL -eq 1 ]]; then
  printf '\n  \033[31m上の点を解消してから起動してください。\033[0m\n\n'
  exit 1
fi
printf '\n'
