#!/usr/bin/env bash
# 「biomni を読み込めません」の切り分け.
#
#   bash scripts/diagnose-app.sh
#
# Docker で動かしている場合はコンテナの中を、そうでなければ手元の Python を見る。
# 「入っているはず」で進めても分からないので、実際に import してみる。
set -uo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

CONTAINER=biomni-app
IN_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  IN_DOCKER=1
fi

# 実行場所を決める
if [[ $IN_DOCKER -eq 1 ]]; then
  RUN() { docker exec "$CONTAINER" "$@"; }
  PY=python
  say "実行場所"; ok "Docker コンテナ $CONTAINER の中を見ます"
else
  if [[ -x .venv/bin/python ]]; then PY="$PWD/.venv/bin/python"
  elif [[ -n "${VIRTUAL_ENV:-}" ]]; then PY="$VIRTUAL_ENV/bin/python"
  else PY=python3; fi
  RUN() { "$@"; }
  say "実行場所"
  warn "コンテナ $CONTAINER は動いていません。手元の Python を見ます"
  ok "$PY"
fi

# ---------------------------------------------------------------- 1. import
say "1. biomni を実際に import してみる"
OUT=$(RUN "$PY" - <<'PYCHECK' 2>&1
import sys, traceback
print("python:", sys.executable)
try:
    import biomni
    from importlib.metadata import version
    try: v = version("biomni")
    except Exception: v = getattr(biomni, "__version__", "?")
    print("OK biomni", v, "at", biomni.__file__)
except BaseException:
    print("NG")
    traceback.print_exc()
PYCHECK
)
echo "$OUT" | sed 's/^/    /'
if grep -q '^OK biomni' <<<"$OUT"; then
  ok "biomni は読み込めています"
  VER=$(grep -o '^OK biomni [^ ]*' <<<"$OUT" | awk '{print $3}')
  [[ "$VER" != "0.0.8" ]] && warn "0.0.8 で検証しています。$VER は未検証です（requirements.txt で固定済み）"
else
  ng "biomni を import できません。上の traceback の最終行が原因です"
fi

# ------------------------------------------------------- 2. ほかの依存も見る
say "2. エージェントに必要な依存"
RUN "$PY" - <<'PYCHECK' 2>&1 | sed 's/^/    /'
import importlib, traceback
mods = ["biomni", "langchain_ollama", "langchain_core", "langgraph",
        "pandas", "langchain_openai", "tqdm", "dotenv",
        "Bio", "bs4", "PyPDF2", "googlesearch", "langchain_anthropic",
        # 関数の中で import されるので、モジュール検査では見つからない。
        # pymed が無いと query_pubmed が呼ばれた瞬間に落ちる
        "pymed", "arxiv"]
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  OK  {m}")
    except BaseException as e:
        print(f"  NG  {m}: {type(e).__name__}: {e}")
PYCHECK

# --------------------------------------------------------------- 3. アプリ
if [[ $IN_DOCKER -eq 1 ]]; then
  PORT=$(sed -n 's/^APP_PORT=//p' .env 2>/dev/null | head -1); PORT="${PORT:-5002}"
  say "3. /api/health"
  if curl -sf -m 5 "http://localhost:${PORT}/api/health" >/tmp/_h.json 2>/dev/null; then
    "$PY" - <<'PYJ' 2>/dev/null || cat /tmp/_h.json
import json
d = json.load(open("/tmp/_h.json"))
b = d.get("biomni", {})
print(f"    biomni: ok={b.get('ok')} version={b.get('version')}")
if b.get("error"):
    print("    エラー:", b["error"].strip().splitlines()[-1])
dep = d.get("dependencies", {})
print(f"    依存: ok={dep.get('ok')} 不足={[m['module'] for m in dep.get('missing', [])]}")
PYJ
    rm -f /tmp/_h.json
  else
    ng "http://localhost:${PORT}/api/health に到達できません"
  fi

  say "4. 起動ログ（biomni 関連）"
  docker logs --tail 200 "$CONTAINER" 2>&1 | grep -iE "biomni|error|traceback" | tail -20 | sed 's/^/    /' \
    || echo "    該当なし"
fi

say "よくある原因"
cat <<'MSG'
  1) イメージの再ビルドが途中で失敗した（ディスク不足など）
       docker compose build --no-cache app && make docker-rebuild
       df -h                 ← 空き容量。イメージ 3GB + モデル 9GB は要る
  2) requirements.txt を変えたのにビルドし直していない
       make docker-rebuild   （--build が付くので通常はこれで足りる）
  3) Docker を使わず、biomni を入れていない Python でアプリを起動している
       bash scripts/doctor.sh   ← どの Python を使っているか
       pip install -r requirements.txt
MSG
