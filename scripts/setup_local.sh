#!/usr/bin/env bash
# ローカル環境のセットアップ.
#
#   bash scripts/setup_local.sh              # 最小構成（テストが通るところまで）
#   bash scripts/setup_local.sh --full       # biomni + Ollama モデル + データセット
#
# Ollama 本体のインストールだけは sudo が要るので自動化しない。手順を表示する。
set -euo pipefail

cd "$(dirname "$0")/.."
FULL=0
[[ "${1:-}" == "--full" ]] && FULL=1

MODEL="${HYPO_MODEL:-qwen3:14b}"
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
VENV=".venv"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

say "Python"
PY=$(command -v python3.11 || command -v python3 || true)
[[ -z "$PY" ]] && { ng "python3 が見つかりません"; exit 1; }
VER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[[ "$(printf '%s\n3.11\n' "$VER" | sort -V | head -1)" != "3.11" ]] && {
  ng "Python 3.11 以上が必要です（検出: ${VER}）"; exit 1; }
ok "$PY ($VER)"

say "仮想環境"
[[ -d "$VENV" ]] || "$PY" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
ok "$VENV"

if [[ $FULL -eq 1 ]]; then
  say "依存（全部）"
  # biomni 0.0.8 は pandas と langchain-openai を宣言していないが、A1 の import に必要。
  # requirements.txt で明示的に入れている。
  pip install --quiet -r requirements.txt
  ok "requirements.txt"
else
  say "依存（最小構成）"
  # 「最小構成でテストが通る」を実際に満たす集合。
  # fastapi / httpx は tests/test_api.py が要る（API を持たずに API は試せない）。
  # ここを削ると collection の時点で止まる（実測で踏んだ）
  pip install --quiet pydantic pyyaml requests pytest fastapi "uvicorn[standard]" httpx
  ok "pydantic / pyyaml / requests / pytest / fastapi / uvicorn / httpx"
  echo "  ※ biomni と Ollama を使うには --full を付けて再実行してください"
fi

say "依存チェック"
python - <<'PYCHECK'
import sys
sys.path.insert(0, ".")
from biomni_hypo.config import AGENT_DEPENDENCIES, install_hint, missing_dependencies

missing = missing_dependencies()
for d in AGENT_DEPENDENCIES:
    print(("  \033[32m✓\033[0m" if d.installed else "  \033[33m·\033[0m"), f"{d.module:20s} {d.why}")
if missing:
    print("\n  エージェントを動かすには:", install_hint(missing))
PYCHECK

say "設定ファイル"
if [[ -f .env ]]; then ok ".env（既存のものを使用）"; else cp .env.example .env; ok ".env を作成"; fi

say "テスト"
# `pytest -q | tail -3` と書くと、pytest がシグナルで殺されたときに
# 何も分からなくなる。パイプのバッファごと出力が消え、シェルには
# `Terminated` の 1 行だけが残る（実測で踏んだ）。
# ログに残し、終了コードからシグナルを名前で報告する。
LOG="logs/pytest.log"
mkdir -p logs
set +e
pytest -q > "$LOG" 2>&1
RC=$?
set -e

if [[ $RC -eq 0 ]]; then
  tail -1 "$LOG"
  ok "pytest"
elif [[ $RC -gt 128 ]]; then
  SIG=$((RC - 128))
  NAME=$(kill -l "$SIG" 2>/dev/null || echo "signal ${SIG}")
  ng "pytest が SIG${NAME} で殺されました（テストの失敗ではありません）"
  echo "      止まった場所（${LOG} の末尾）:"
  tail -15 "$LOG" | sed 's/^/        /'
  echo
  echo "      よくある原因:"
  echo "        SIGKILL(9)  メモリ不足。dmesg -T | tail や journalctl -k | tail で確認"
  echo "        SIGTERM(15) 誰かがプロセスグループごと止めている。"
  echo "                    タイムアウト付きの実行や、常駐サービスの停止処理を確認"
  exit 1
else
  tail -20 "$LOG"
  ng "pytest が失敗しました（全文: ${LOG}）"
  exit 1
fi

if [[ $FULL -eq 1 ]]; then
  say "Ollama"
  if curl -sf -m 5 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    ok "$OLLAMA_URL に到達"
    if curl -sf -m 5 "$OLLAMA_URL/api/tags" | grep -q "\"$MODEL\""; then
      ok "$MODEL 取得済み"
    else
      echo "  $MODEL を取得します..."
      ollama pull "$MODEL" && ok "$MODEL"
    fi
  else
    ng "$OLLAMA_URL に到達できません"
    cat <<'MSG'

  Ollama を入れてから、もう一度 --full で実行してください:

    macOS / Linux:  curl -fsSL https://ollama.com/install.sh | sh
    Homebrew:       brew install ollama
    起動:           ollama serve

  そのあと:         ollama pull qwen3:14b
MSG
    exit 1
  fi

  say "データセット（許可リストのうち最小限）"
  # 失敗しても止めない（データセット無しでもアプリは動く）。ただし
  # 何でも「ネットワークを確認」と言わないこと。理由はスクリプトが出す
  if python scripts/fetch_datasets.py --only gwas_catalog.pkl gene_info.parquet; then
    ok "データセット"
  else
    ng "データセット取得に失敗しました（上の理由を見てください）"
    echo "      データセットが無くてもアプリは起動します。あとで:  make fetch"
  fi
fi

say "完了"
cat <<MSG
  次にやること:

    source $VENV/bin/activate
    jupyter lab notebooks/        # 00 -> 01 の順で実行（01 が最重要）
    make api                      # http://localhost:5002/docs

  Ollama 無しでも動くもの:
    pytest -q                              # 全テスト
    notebooks/03_evidence_extraction.ipynb # 根拠の抽出と検証
MSG
