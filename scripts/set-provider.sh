#!/usr/bin/env bash
# LLM プロバイダを切り替える（.env を書き換える）.
#
#   bash scripts/set-provider.sh both   --key sk-ant-... --port 8003
#   bash scripts/set-provider.sh claude --key sk-ant-... --model claude-sonnet-5
#   bash scripts/set-provider.sh ollama --model qwen3:14b
#
# both は「Ollama と Claude の両方を選択肢に出す」モード。既定のプロバイダは
# --default {ollama|claude} で決める（省略時は ollama）。実行ごとの切り替えは
# 画面のモデル選択で行う（apply_model_selection がモデル名からプロバイダを決める）。
#
# 揃える必要がある変数が 4 つあり、手で書くと食い違う:
#   HYPO_PROVIDER      既定のプロバイダ
#   HYPO_MODEL         既定のモデル名
#   BIOMNI_SOURCE      biomni の default_config が向く先（docs/design/04 §4.3）
#   COMPOSE_PROFILES   Docker で ollama コンテナを起動するか
set -uo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

usage() {
  echo "使い方: bash scripts/set-provider.sh {both|claude|ollama} \\" >&2
  echo "          [--model M] [--claude-model M] [--key K] [--port P] [--default {ollama|claude}]" >&2
}

MODE="${1:-}"; shift || true
MODEL=""; CLAUDE_MODEL=""; KEY=""; PORT=""; DEFAULT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)        MODEL="$2"; shift 2 ;;
    --claude-model) CLAUDE_MODEL="$2"; shift 2 ;;
    --key)          KEY="$2"; shift 2 ;;
    --port)         PORT="$2"; shift 2 ;;
    --default)      DEFAULT="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; usage; exit 1 ;;
  esac
done

case "$MODE" in
  both|all|hybrid)  MODE=both ;;
  claude|anthropic) MODE=anthropic ;;
  ollama|local)     MODE=ollama ;;
  *) usage; exit 1 ;;
esac

case "${DEFAULT:-}" in
  claude|anthropic) DEFAULT=anthropic ;;
  ollama|local)     DEFAULT=ollama ;;
  "")               DEFAULT="" ;;
  *) echo "--default は ollama か claude を指定してください" >&2; exit 1 ;;
esac

[[ -f .env ]] || { cp .env.example .env; ok ".env を作成しました"; }

# .env の 1 行を書き換える（無ければ足す、重複していれば 1 行に潰す）。
# 値に # や / が入るので sed の区切り文字に依存しない実装にする。
set_env() {
  KEY_NAME="$1" KEY_VALUE="$2" python3 - <<'PY'
import os, pathlib
key, value = os.environ["KEY_NAME"], os.environ["KEY_VALUE"]
path = pathlib.Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
out, written = [], False
for line in lines:
    stripped = line.lstrip("#").lstrip()
    if stripped.startswith(f"{key}="):
        if written:
            continue          # 重複行は捨てる
        out.append(f"{key}={value}")
        written = True
    else:
        out.append(line)
if not written:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

has_key() { grep -qE '^ANTHROPIC_API_KEY=.+' .env; }

[[ -n "$KEY" ]] && set_env ANTHROPIC_API_KEY "$KEY"

say "モード: $MODE"
case "$MODE" in
  anthropic)
    MODEL="${MODEL:-${CLAUDE_MODEL:-claude-opus-5}}"
    set_env HYPO_PROVIDER anthropic
    set_env HYPO_MODEL "$MODEL"
    set_env BIOMNI_LLM "$MODEL"
    set_env BIOMNI_SOURCE Anthropic
    set_env LLM_SOURCE Anthropic
    set_env COMPOSE_PROFILES ""      # ollama コンテナを起動しない
    ok "HYPO_PROVIDER=anthropic / HYPO_MODEL=$MODEL / BIOMNI_SOURCE=Anthropic"
    ok "COMPOSE_PROFILES=（空）→ Docker で ollama を起動しません"
    warn "質問文と実行結果が Anthropic に送信されます（オフラインモードとは併用不可）"
    ;;
  ollama)
    MODEL="${MODEL:-qwen3:14b}"
    set_env HYPO_PROVIDER ollama
    set_env HYPO_MODEL "$MODEL"
    set_env BIOMNI_LLM "$MODEL"
    set_env BIOMNI_SOURCE Ollama
    set_env LLM_SOURCE Ollama
    set_env COMPOSE_PROFILES ollama
    ok "HYPO_PROVIDER=ollama / HYPO_MODEL=$MODEL / BIOMNI_SOURCE=Ollama"
    ok "COMPOSE_PROFILES=ollama → Docker で ollama も起動します"
    ;;
  both)
    DEFAULT="${DEFAULT:-ollama}"
    MODEL="${MODEL:-qwen3:14b}"
    CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-5}"
    # ollama コンテナは常に起動する（Claude を既定にしても選択肢として残す）
    set_env COMPOSE_PROFILES ollama
    set_env HYPO_MODEL "$MODEL"
    if [[ "$DEFAULT" == anthropic ]]; then
      set_env HYPO_PROVIDER anthropic
      set_env HYPO_MODEL "$CLAUDE_MODEL"
      set_env BIOMNI_LLM "$CLAUDE_MODEL"
      set_env BIOMNI_SOURCE Anthropic
      set_env LLM_SOURCE Anthropic
      ok "既定は Claude（$CLAUDE_MODEL）。Ollama の $MODEL も選べます"
    else
      set_env HYPO_PROVIDER ollama
      set_env BIOMNI_LLM "$MODEL"
      set_env BIOMNI_SOURCE Ollama
      set_env LLM_SOURCE Ollama
      ok "既定は Ollama（$MODEL）。Claude の $CLAUDE_MODEL も選べます"
    fi
    set_env HYPO_OFFLINE_MODE false
    ok "COMPOSE_PROFILES=ollama → Docker で ollama も起動します"
    warn "Claude を選んだ実行では、質問文と実行結果が Anthropic に送信されます"
    ;;
esac

if has_key; then
  ok "ANTHROPIC_API_KEY 設定済み"
elif [[ "$MODE" != ollama ]]; then
  ng "ANTHROPIC_API_KEY が未設定です（Claude のモデルは一覧に出ますが選べません）"
  echo "      bash scripts/set-provider.sh $([[ $MODE == both ]] && echo both || echo claude) --key sk-ant-..."
  echo "      または .env に直接  ANTHROPIC_API_KEY=sk-ant-...  を書く"
fi

if [[ -n "$PORT" ]]; then
  set_env APP_PORT "$PORT"
  ok "APP_PORT=$PORT"
fi

say "いまの .env"
grep -E '^(HYPO_PROVIDER|HYPO_MODEL|BIOMNI_LLM|BIOMNI_SOURCE|COMPOSE_PROFILES|APP_PORT|APP_BIND)=' .env |
  sed 's/^/  /'
has_key && echo "  ANTHROPIC_API_KEY=...（設定済み・非表示）"

PORT_NOW=$(sed -n 's/^APP_PORT=//p' .env | head -1); PORT_NOW="${PORT_NOW:-8000}"
say "反映する"
cat <<MSG
  Docker で常設している場合:
      make docker-rebuild            （または sudo systemctl restart biomni-hypo）
  Docker を使わない場合:
      bash scripts/start.sh

  そのあと  http://localhost:${PORT_NOW}
  モデル選択のプルダウンで「ローカル（Ollama）」と「クラウド（Claude API）」を
  実行ごとに切り替えられます。
MSG
