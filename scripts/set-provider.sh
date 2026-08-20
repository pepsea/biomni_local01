#!/usr/bin/env bash
# LLM プロバイダを切り替える（.env を書き換える）.
#
#   bash scripts/set-provider.sh claude --key sk-ant-... --port 8003
#   bash scripts/set-provider.sh claude --model claude-sonnet-5
#   bash scripts/set-provider.sh ollama --model qwen3:14b
#
# 揃える必要がある変数が 4 つあり、手で書くと食い違う:
#   HYPO_PROVIDER      アプリが使うプロバイダ
#   HYPO_MODEL         モデル名
#   BIOMNI_SOURCE      biomni の default_config が向く先（docs/design/04 §4.3）
#   COMPOSE_PROFILES   Docker で ollama コンテナを起動するか
set -uo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

PROVIDER="${1:-}"; shift || true
MODEL=""; KEY=""; PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --key)   KEY="$2"; shift 2 ;;
    --port)  PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; exit 1 ;;
  esac
done

case "$PROVIDER" in
  claude|anthropic) PROVIDER=anthropic ;;
  ollama|local)     PROVIDER=ollama ;;
  *) echo "使い方: bash scripts/set-provider.sh {claude|ollama} [--model M] [--key K] [--port P]" >&2; exit 1 ;;
esac

[[ -f .env ]] || { cp .env.example .env; ok ".env を作成しました"; }

set_env() {
  local key="$1" value="$2"
  if grep -qE "^#?\s*${key}=" .env; then
    sed -i "s#^#\?\s*${key}=.*#${key}=${value}#" .env
    # 上の sed が効かない環境向けの保険
    grep -qE "^${key}=" .env || printf '%s=%s\n' "$key" "$value" >> .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
  # 重複行を最後のものに寄せる
  local last
  last=$(grep -nE "^${key}=" .env | tail -1 | cut -d: -f1)
  if [[ -n "$last" ]]; then
    awk -v k="$key" -v keep="$last" 'BEGIN{n=0} { n++; if ($0 ~ "^"k"=" && n != keep) next; print }' .env > .env.tmp
    mv .env.tmp .env
  fi
  sed -i "s#^${key}=.*#${key}=${value}#" .env
}

say "プロバイダ: $PROVIDER"
if [[ "$PROVIDER" == anthropic ]]; then
  MODEL="${MODEL:-claude-opus-5}"
  set_env HYPO_PROVIDER anthropic
  set_env HYPO_MODEL "$MODEL"
  set_env BIOMNI_LLM "$MODEL"
  set_env BIOMNI_SOURCE Anthropic
  set_env COMPOSE_PROFILES ""      # ollama コンテナを起動しない
  [[ -n "$KEY" ]] && set_env ANTHROPIC_API_KEY "$KEY"
  ok "HYPO_PROVIDER=anthropic / HYPO_MODEL=$MODEL / BIOMNI_SOURCE=Anthropic"
  ok "COMPOSE_PROFILES=（空）→ Docker で ollama を起動しません"
  if grep -qE '^ANTHROPIC_API_KEY=sk-' .env; then
    ok "ANTHROPIC_API_KEY 設定済み"
  else
    ng "ANTHROPIC_API_KEY が未設定です"
    echo "      bash scripts/set-provider.sh claude --key sk-ant-..."
    echo "      または .env に直接  ANTHROPIC_API_KEY=sk-ant-...  を書く"
  fi
  warn "質問文と実行結果が Anthropic に送信されます（オフラインモードとは併用不可）"
else
  MODEL="${MODEL:-qwen3:14b}"
  set_env HYPO_PROVIDER ollama
  set_env HYPO_MODEL "$MODEL"
  set_env BIOMNI_LLM "$MODEL"
  set_env BIOMNI_SOURCE Ollama
  set_env COMPOSE_PROFILES ollama
  ok "HYPO_PROVIDER=ollama / HYPO_MODEL=$MODEL / BIOMNI_SOURCE=Ollama"
  ok "COMPOSE_PROFILES=ollama → Docker で ollama も起動します"
fi

if [[ -n "$PORT" ]]; then
  set_env APP_PORT "$PORT"
  ok "APP_PORT=$PORT"
fi

say "いまの .env"
grep -E '^(HYPO_PROVIDER|HYPO_MODEL|BIOMNI_LLM|BIOMNI_SOURCE|COMPOSE_PROFILES|APP_PORT|APP_BIND)=' .env |
  sed 's/^/  /'
grep -qE '^ANTHROPIC_API_KEY=sk-' .env && echo "  ANTHROPIC_API_KEY=sk-...（設定済み・非表示）"

PORT_NOW=$(sed -n 's/^APP_PORT=//p' .env | head -1); PORT_NOW="${PORT_NOW:-8000}"
say "反映する"
cat <<MSG
  Docker で常設している場合:
      make docker-rebuild            （または sudo systemctl restart biomni-hypo）
  Docker を使わない場合:
      bash scripts/start.sh

  そのあと  http://localhost:${PORT_NOW}
MSG
