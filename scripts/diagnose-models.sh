#!/usr/bin/env bash
# 「使えるモデルがあるのに選択できない」の切り分け.
#
#   bash scripts/diagnose-models.sh          # .env の APP_PORT を使う
#   bash scripts/diagnose-models.sh 8003
#
# 動いているアプリ自身に聞く。ここで見えているものが、画面に出ているもの。
set -uo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

PORT="${1:-}"
if [[ -z "$PORT" ]]; then
  PORT=$(sed -n 's/^APP_PORT=//p' .env 2>/dev/null | head -1); PORT="${PORT:-5002}"
fi
BASE="http://localhost:${PORT}"

if ! curl -sf -m 5 "${BASE}/api/health" -o /tmp/_hz.json 2>/dev/null; then
  ng "${BASE} に到達できません。アプリは動いていますか？"
  echo "      make service-status   /   make docker-ps   /   bash scripts/start.sh"
  exit 1
fi

PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python

say "動いているビルド"
"$PY" - <<'PYJ'
import json
d = json.load(open("/tmp/_hz.json"))
print(f"  version={d.get('version')}  build={d.get('build') or '(不明)'}")
b = d.get("biomni") or {}
print(f"  biomni: ok={b.get('ok')} version={b.get('version')}")
o = d.get("ollama") or {}
print(f"  ollama: reachable={o.get('reachable')}  base_url={o.get('base_url')}")
if o.get("error"):
    print("          error:", str(o['error'])[:120])
m = d.get("models") or {}
print(f"  configured={m.get('configured')}  default={m.get('default')}")
PYJ

# Ollama がコンテナで動いていると、モデルの置き場がホストと別になる。
# ホストで ollama pull しても、コンテナの中からは見えない。
# 「mac では動くが Linux では使えるモデルが無い」の典型（docs/design/35）
OLLAMA_CONTAINERS=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  OLLAMA_CONTAINERS=$(docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null \
    | grep -i ollama | awk -F'\t' '{print $1}' | tr '\n' ' ')
fi
export OLLAMA_CONTAINERS

if [[ -n "${OLLAMA_CONTAINERS// /}" ]]; then
  say "Ollama のコンテナ"
  docker ps --format '  {{.Names}}  {{.Image}}  {{.Ports}}' 2>/dev/null | grep -i ollama
  echo "      モデルはコンテナの中に置かれます。ホストで pull したものは見えません。"
fi

say "モデル一覧（アプリが見ているもの）"
if ! curl -sf -m 20 "${BASE}/api/models" -o /tmp/_mz.json 2>/dev/null; then
  ng "/api/models が返りません（Ollama への問い合わせで詰まっている可能性）"
  exit 1
fi
"$PY" - <<'PYJ'
import json
import os

d = json.load(open("/tmp/_mz.json"))
rows = d.get("models", [])
usable = [m for m in rows if m["installed"] and m["allowed"]]
print(f"  選べる: {len(usable)} 件 / 一覧: {len(rows)} 件   default={d.get('default')!r}")
print()
print(f"  {'':2s} {'モデル':32s} {'種別':8s} {'状態':22s} 理由")
for m in rows:
    if m["installed"] and m["allowed"]:
        mark, state = "✓", "選択可"
    elif not m["installed"]:
        mark, state = "…", "未取得"
    else:
        mark, state = "✕", "ポリシーで不可"
    reason = m.get("reason") or m.get("license") or ""
    kind = "ローカル" if m["local"] else "クラウド"
    print(f"  {mark:2s} {m['name']:32s} {kind:8s} {state:22s} {reason[:40]}")
print()
installed_local = [m for m in rows if m["local"] and m["installed"]]
if not installed_local:
    print("  → アプリが見ている Ollama には、モデルが 1 件もありません。")
    containers = [c for c in os.environ.get("OLLAMA_CONTAINERS", "").split() if c]
    if containers:
        # コンテナで動いている場合、pull 先を間違えているのがほぼ確実
        name = containers[0]
        print("     Ollama はコンテナで動いています。モデルはコンテナの中に入れてください。")
        print(f"       docker exec -it {name} ollama list")
        print(f"       docker exec -it {name} ollama pull qwen3:14b     # Apache-2.0・推奨")
        print()
        print("     ホスト側の `ollama list` に見えていても、コンテナからは見えません。")
        print("     置き場所が別だからです（mac で動いて Linux で動かない、の典型）。")
    else:
        print("     手元で `ollama list` に見えているなら、別の Ollama を見ています。")
        print("     ・別ポートを指している        → bash scripts/set-provider.sh ollama")
        print("     ・コンテナ版が残っている      → docker ps | grep ollama")
        print("     ・別ユーザーの ollama serve   → どちらか一方に寄せる")
elif not usable:
    print("  → 選べるモデルが 0 件です（商用ポリシーで全部弾かれています）。")
    print("     ollama pull qwen3:14b      （Apache-2.0・推奨）")
    print("     または  bash scripts/set-provider.sh both --key sk-ant-...")
elif d.get("default") not in {m["name"] for m in usable}:
    print(f"  → default={d.get('default')!r} が選択可の中にありません。ここが原因です。")
else:
    print(f"  → 既定 {d.get('default')!r} は選択可。画面で選べないなら、")
    print("     ブラウザが古い HTML を掴んでいます（強制リロード: Ctrl/Cmd-Shift-R）。")
    print("     ヘッダーの build が git の HEAD と違うなら再ビルドしてください:")
    print("         git rev-parse --short HEAD")
    print("         make update")
PYJ
rm -f /tmp/_hz.json /tmp/_mz.json
