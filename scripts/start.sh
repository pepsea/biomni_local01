#!/usr/bin/env bash
# Web アプリを起動する。起動前に環境を確認して、足りないものを具体的に指摘する。
#
#   bash scripts/start.sh                 # http://localhost:8000
#   bash scripts/start.sh --port 9000
#   bash scripts/start.sh --check         # 確認だけして起動しない
#   bash scripts/start.sh --reload        # コード変更を自動反映（開発用）
set -uo pipefail
cd "$(dirname "$0")/.."

PORT=8000
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
        AGENT_DEPENDENCIES, API_DEPENDENCIES, install_hint, missing_dependencies,
    )
except Exception as exc:
    print("FATAL", exc); raise SystemExit(0)
print("API_MISSING", install_hint(missing_dependencies(API_DEPENDENCIES)))
print("AGENT_MISSING", install_hint(missing_dependencies(AGENT_DEPENDENCIES)))
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

if [[ $CHECK_ONLY -eq 1 ]]; then
  say "確認のみ（--check）"; exit 0
fi

say "起動"
echo "  ブラウザで  http://localhost:$PORT  を開いてください（停止は Ctrl-C）"
echo
exec "$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" $RELOAD
