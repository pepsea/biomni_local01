#!/usr/bin/env bash
# どの Python を使っているかを突き止める.
#
#   bash scripts/doctor.sh
#
# 「pip install したのに ModuleNotFoundError」はほぼ全部、
# インストール先の Python と実行している Python が違うことが原因。
# 候補ごとに「その Python に何が入っているか」を出す。
set -uo pipefail
cd "$(dirname "$0")/.."

hdr() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

hdr "いま PATH で見えているもの"
for cmd in python python3 pip pip3 jupyter; do
  path=$(command -v "$cmd" 2>/dev/null)
  if [[ -n "$path" ]]; then
    printf '  %-8s %s\n' "$cmd" "$path"
  else
    printf '  %-8s \033[33m(なし)\033[0m\n' "$cmd"
  fi
done
printf '  %-8s %s\n' 'VIRTUAL_ENV' "${VIRTUAL_ENV:-（未設定 = 仮想環境が有効化されていない）}"

# 調べる対象の Python を集める
CANDIDATES=()
add() { [[ -x "$1" ]] && CANDIDATES+=("$1"); }
[[ -n "${VIRTUAL_ENV:-}" ]] && add "$VIRTUAL_ENV/bin/python"
add "./.venv/bin/python"
add "$HOME/projects/jupyter/.venv/bin/python"
for c in python3 python; do
  p=$(command -v "$c" 2>/dev/null) && add "$p"
done
# Jupyter カーネルが指す Python も候補に入れる
if command -v jupyter >/dev/null 2>&1; then
  while read -r kp; do add "$kp"; done < <(
    jupyter kernelspec list --json 2>/dev/null |
      python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
for spec in d.get('kernelspecs',{}).values():
    argv = spec.get('spec',{}).get('argv') or []
    if argv: print(argv[0])
" 2>/dev/null)
fi

# 重複を除く。venv は実体が同じバイナリを指すので、sys.prefix（= site-packages の場所）で見分ける
UNIQ=(); SEEN=()
for c in "${CANDIDATES[@]:-}"; do
  [[ -z "$c" ]] && continue
  prefix=$("$c" -c 'import sys;print(sys.prefix)' 2>/dev/null) || continue
  dup=0
  for s in "${SEEN[@]:-}"; do [[ "$s" == "$prefix" ]] && dup=1; done
  if [[ $dup -eq 0 ]]; then
    SEEN+=("$prefix")
    UNIQ+=("$("$c" -c 'import sys;print(sys.executable)' 2>/dev/null)")
  fi
done

hdr "見つかった Python と、その中身"
for py in "${UNIQ[@]:-}"; do
  ver=$("$py" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)
  kind=$("$py" -c 'import sys;print("仮想環境" if sys.prefix!=sys.base_prefix else "システム")' 2>/dev/null)
  printf '\n  \033[1m%s\033[0m  (Python %s / %s)\n' "$py" "$ver" "$kind"
  "$py" - <<'PYCHECK' 2>/dev/null || echo "      （確認できませんでした）"
import importlib.util as u
groups = {
    "アプリ本体": [("pydantic","pydantic"),("yaml","pyyaml"),("requests","requests")],
    "API サーバ": [("fastapi","fastapi"),("uvicorn","uvicorn[standard]")],
    "エージェント": [("biomni","biomni"),("langchain_ollama","langchain-ollama"),
                     ("langgraph","langgraph"),("pandas","pandas"),("tqdm","tqdm"),
                     ("langchain_openai","langchain-openai"),("dotenv","python-dotenv")],
    "ツール": [("Bio","biopython"),("bs4","beautifulsoup4"),
               ("PyPDF2","PyPDF2"),("googlesearch","googlesearch-python")],
    "Claude API": [("langchain_anthropic","langchain-anthropic")],
}
missing = []
for label, mods in groups.items():
    have = [m for m, _ in mods if u.find_spec(m) is not None]
    lack = [(m, p) for m, p in mods if u.find_spec(m) is None]
    missing += [p for _, p in lack]
    mark = "\033[32m✓\033[0m" if not lack else "\033[33m·\033[0m"
    print(f"      {mark} {label:12s} {len(have)}/{len(mods)}"
          + (f"   足りない: {', '.join(m for m, _ in lack)}" if lack else ""))
if missing:
    import sys
    print("      → この Python に入れるなら:")
    print(f"        {sys.executable} -m pip install " + " ".join(sorted(set(missing))))
else:
    print("      \033[32m→ このリポジトリを動かすのに必要なものは揃っています\033[0m")
PYCHECK
done

hdr "LLM の準備状況"
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  ok "ANTHROPIC_API_KEY 設定済み（Claude API が使えます）"
else
  ng "ANTHROPIC_API_KEY が未設定 → Claude API は選べません"
  echo "      export ANTHROPIC_API_KEY=sk-ant-...    （console.anthropic.com で発行）"
fi
if curl -sf -m 3 "${OLLAMA_BASE_URL:-http://localhost:11434}/api/tags" >/dev/null 2>&1; then
  n=$(curl -sf -m 3 "${OLLAMA_BASE_URL:-http://localhost:11434}/api/tags" | grep -o '"name"' | wc -l | tr -d ' ')
  ok "Ollama に到達（モデル ${n} 件）"
else
  ng "Ollama に到達できません（${OLLAMA_BASE_URL:-http://localhost:11434}）"
  echo "      ollama serve  を起動してから  ollama pull qwen3:14b"
fi
echo "  ※ どちらか一方あれば動きます。両方無いとモデルを選べません"

hdr "この Python に入れる、を確実にやる方法"
cat <<'MSG'
  シェルから:
      /path/to/python -m pip install ...        ← pip ではなく「python -m pip」を使う
      （pip がどの Python に紐づくかは PATH 次第で当てにならない）

  Jupyter のセルから:
      %pip install biopython                    ← カーネルの Python に入る（推奨）
      import sys; print(sys.executable)         ← カーネルの Python を確認

  このアプリを起動するとき:
      bash scripts/start.sh                     ← 有効な venv か ./.venv を自動で選び、
                                                   足りない依存をその場で指摘します
MSG

hdr "このリポジトリを動かすなら"
if [[ -x ".venv/bin/python" ]]; then
  ok "./.venv があります:  source .venv/bin/activate && pip install -r requirements.txt"
else
  ng "./.venv がありません:  bash scripts/setup_local.sh --full"
fi
