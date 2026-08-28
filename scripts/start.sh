#!/usr/bin/env bash
# Web アプリを起動する。起動前に環境を確認して、足りないものを具体的に指摘する。
#
#   bash scripts/start.sh                 # http://localhost:8000
#   bash scripts/start.sh --port 9000     # ポートを変える
#                                         # （.env の APP_PORT でも指定できます）
#   bash scripts/start.sh --check         # 確認だけして起動しない
#   bash scripts/start.sh --reload        # コード変更を自動反映（開発用）
set -uo pipefail
cd "$(dirname "$0")/.."

# .env の APP_PORT があればそれを既定にする（Docker 版と揃える）
PORT="${APP_PORT:-8000}"
if [[ -z "${APP_PORT:-}" && -f .env ]]; then
  PORT=$(sed -n 's/^APP_PORT=//p' .env | head -1)
  PORT="${PORT:-8000}"
fi
CHECK_ONLY=0
RELOAD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    --reload) RELOAD="--reload"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

say "Python 環境"
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  PY="python"; ok "有効な仮想環境: $VIRTUAL_ENV"
elif [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"; ok "./.venv を使います"
else
  PY="python3"
  warn "仮想環境が見つかりません（bash scripts/setup_local.sh を推奨）"
fi

say "依存パッケージ"
DEPS=$("$PY" - <<'PYCHECK'
import sys
sys.path.insert(0, ".")
try:
    from biomni_hypo.config import (
        AGENT_DEPENDENCIES, API_DEPENDENCIES, TOOL_DEPENDENCIES,
        install_hint, missing_dependencies,
    )
except Exception as exc:
    print("FATAL", exc); raise SystemExit(0)
print("API_MISSING", install_hint(missing_dependencies(API_DEPENDENCIES)))
print("AGENT_MISSING", install_hint(missing_dependencies(AGENT_DEPENDENCIES)))
print("TOOL_MISSING", install_hint(missing_dependencies(TOOL_DEPENDENCIES)))
PYCHECK
)
if grep -q '^FATAL' <<<"$DEPS"; then
  ng "biomni_hypo を import できません: $(sed -n 's/^FATAL //p' <<<"$DEPS")"
  echo "      リポジトリのルートで実行していますか？  pip install -r requirements.txt"
  exit 1
fi
API_MISSING=$(sed -n 's/^API_MISSING //p' <<<"$DEPS")
AGENT_MISSING=$(sed -n 's/^AGENT_MISSING //p' <<<"$DEPS")
if [[ -n "$API_MISSING" ]]; then
  ng "API サーバの依存が足りません:  $API_MISSING"; exit 1
fi
ok "API サーバの依存 OK"
if [[ -n "$AGENT_MISSING" ]]; then
  warn "エージェントの依存が足りません（起動はできますが実行は失敗します）:"
  echo "      $AGENT_MISSING"
else
  ok "エージェントの依存 OK"
fi
# ツールの依存は、無くても起動はできる。ただし該当ツールは自動で無効になり
# （docs/design/20）、エージェントが引ける情報源が減る。黙って減らさない
TOOL_MISSING=$(sed -n 's/^TOOL_MISSING //p' <<<"$DEPS")
if [[ -n "$TOOL_MISSING" ]]; then
  warn "ツールの依存が足りません。該当ツールは無効になります:"
  echo "      $TOOL_MISSING"
  echo "      まとめて入れる:  pip install -r requirements.txt"
else
  ok "ツールの依存 OK"
fi

say "LLM"
"$PY" - <<'PYCHECK'
import sys
sys.path.insert(0, ".")
from biomni_hypo.config import Settings
from biomni_hypo.models import list_local_models
from biomni_hypo.policy import ResourcePolicy

settings = Settings()
catalog = list_local_models(
    settings, ResourcePolicy.load(settings.policy_path), fetch_context_length=False
)
sel = [m.name for m in catalog.selectable]
if sel:
    print(f"  \033[32m✓\033[0m 使えるモデル {len(sel)} 件: " + ", ".join(sel[:5]))
    default = catalog.default(settings.model)
    print(f"      既定: {default.name}（{'ローカル' if default.local else 'クラウド'}）")
else:
    print("  \033[31m✗\033[0m 使えるモデルがありません")
    if not catalog.reachable:
        print(f"      Ollama に到達できません（{catalog.base_url}）→  ollama serve")
    print("      ローカルで使う   :  ollama pull qwen3:14b")
    print("      Claude API を使う:  export ANTHROPIC_API_KEY=sk-ant-...")
PYCHECK

# ポートを掴んでいるプロセスを 1 行で説明する。
# macOS には ss が無く Linux には lsof が無いことがあるので両対応にする。
port_holder() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $1" (PID "$2", "$3")"; exit}'
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$port" 2>/dev/null | awk 'NR>1 {print $NF; exit}'
  fi
}

# その port で応答しているのが「このアプリ自身」かどうか
already_ours() {
  curl -sf -m 2 "http://127.0.0.1:$1/api/health" 2>/dev/null | grep -q '"policy_version"'
}

say "待ち受け"
if already_ours "$PORT"; then
  ok "ポート $PORT では既にこのアプリが動いています"
  echo
  echo "  そのまま  http://localhost:$PORT  を開いてください。"
  echo
  echo "  入れ替えたい場合:"
  echo "      make docker-rebuild                 （Docker で常設している場合）"
  echo "      sudo systemctl restart biomni-hypo  （systemd で常設している場合）"
  echo "  別のポートで並行して動かす場合:"
  echo "      bash scripts/start.sh --port $((PORT + 1))"
  exit 0
fi

HOLDER="$(port_holder "$PORT")"
if [[ -n "$HOLDER" ]]; then
  ng "ポート $PORT は既に使われています: $HOLDER"
  echo
  echo "  掴んでいるものを確認する:"
  echo "      lsof -nP -iTCP:$PORT -sTCP:LISTEN"
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Ports}}\t{{.Names}}' 2>/dev/null | grep -q ":${PORT}->"; then
    echo
    echo "  Docker のコンテナが使っています:"
    docker ps --format '      {{.Names}}  {{.Ports}}' 2>/dev/null | grep ":${PORT}->"
    echo "      make docker-down       （止める）"
    echo "      make docker-logs       （ログを見る）"
  fi
  echo
  echo "  どれかを選んでください:"
  echo "      1) 別のポートで起動する     bash scripts/start.sh --port $((PORT + 1))"
  echo "      2) 掴んでいるものを止める   kill <PID>  /  make docker-down"
  echo "      3) 既定のポートを変える     .env の APP_PORT を書き換える"
  exit 1
fi
ok "http://localhost:$PORT"

if [[ $CHECK_ONLY -eq 1 ]]; then
  say "確認のみ（--check）"; exit 0
fi

say "起動"
echo "  ブラウザで  http://localhost:$PORT  を開いてください（停止は Ctrl-C）"
echo
exec "$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" $RELOAD
