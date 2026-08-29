#!/usr/bin/env bash
# 「Ollama は起動しているのにアプリからは未接続」を切り分ける.
#
#   bash scripts/diagnose-ollama.sh
#
# 見るのは 4 か所。どこで切れているかで対処が変わる。
#   1. ホストから Ollama            -> Ollama 自体が動いているか
#   2. アプリの設定 (.env)          -> どの URL を見に行くか
#   3. 読み込まれているモデル        -> 他のツールと取り合っていないか
#   4. コンテナから Ollama          -> Docker の場合。localhost はコンテナ自身を指す
set -uo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

probe() {  # URL -> 到達できればモデル数を表示
  local url="$1" body
  body=$(curl -sf -m 3 "${url}/api/tags" 2>/dev/null) || return 1
  printf '%s' "$body" | grep -o '"name"' | wc -l | tr -d ' '
}

probe_in_container() {  # コンテナの中から見る
  docker exec biomni-app sh -lc \
    "curl -sf -m 3 \"$1/api/tags\" >/dev/null 2>&1 && echo OK || echo NG" 2>/dev/null
}

# 環境変数を優先する（テストから別ポートを指せるように）
PORT="${OLLAMA_PORT:-$(sed -n 's/^OLLAMA_PORT=//p' .env 2>/dev/null | head -1)}"; PORT="${PORT:-11434}"
CONFIGURED="${OLLAMA_BASE_URL:-$(sed -n 's/^OLLAMA_BASE_URL=//p' .env 2>/dev/null | head -1)}"
CONFIGURED="${CONFIGURED:-http://localhost:${PORT}}"

# ---------------------------------------------------------------- 1. ホスト
say "1. ホストから Ollama が見えるか"
HOST_OK=0
for url in "http://localhost:${PORT}" "http://127.0.0.1:${PORT}"; do
  if n=$(probe "$url"); then ok "$url に到達（モデル ${n} 件）"; HOST_OK=1; break; fi
done
if [[ $HOST_OK -eq 0 ]]; then
  ng "ホストの :${PORT} に Ollama が見つかりません"
  echo "      ollama serve  を起動してください"
  if command -v lsof >/dev/null 2>&1; then
    other=$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -i ollama | awk '{print $1" "$9}' | head -3)
    [[ -n "$other" ]] && { warn "別のポートで待ち受けているようです:"; printf '      %s\n' "$other"; }
  fi
  exit 1
fi

# ---------------------------------------------------------------- 2. 設定
say "2. アプリが見に行く URL"
echo "  .env の OLLAMA_BASE_URL = ${CONFIGURED}"


# ------------------------------------------------ 3. 他のツールとの取り合い
# 同じ Ollama を別のツールと共有していると、モデルの入れ替えとメモリの
# 取り合いが起きる。生成が極端に遅くなり、こちらは wallclock / max_steps で
# 打ち切られて <solution> に到達しない、という形で出る（docs/design/32）。
say "3. いま Ollama に読み込まれているモデル"
WANT="${HYPO_MODEL:-$(sed -n 's/^HYPO_MODEL=//p' .env 2>/dev/null | head -1)}"; WANT="${WANT:-qwen3:14b}"
PS_JSON=$(curl -sf -m 3 "${FOUND:-$CONFIGURED}/api/ps" 2>/dev/null)
if [[ -z "$PS_JSON" ]]; then
  warn "/api/ps を取得できませんでした（古い Ollama では未対応です）"
else
  # コロンの後ろの空白は実装によって有無が変わる。空白なし前提だと 0 件に見える
  LOADED=$(printf '%s' "$PS_JSON" \
    | grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | sed 's/.*"\([^"]*\)"$/\1/' | sort -u)
  if [[ -z "$LOADED" ]]; then
    ok "読み込み済みのモデルはありません（最初の 1 回は読み込みに時間がかかります）"
  else
    printf '      %s
' $LOADED
    OTHERS=$(printf '%s
' $LOADED | grep -vx "$WANT" | tr '\n' ' ')
    if [[ -n "${OTHERS// /}" ]]; then
      warn "このアプリが使う ${WANT} 以外も読み込まれています: ${OTHERS}"
      echo "      同じ Ollama を他のツールと共有すると、こうなります:"
      echo "        - メモリを取り合い、層が CPU に溢れて生成が数倍〜数十倍遅くなる"
      echo "        - モデルを切り替えるたびに読み込み直しが入る"
      echo "        - 同じモデルへの同時要求は順番待ちになる"
      echo "      遅くなると、こちらは打ち切られて <solution> に届かず、"
      echo "      「回答が得られませんでした」になります。"
      echo
      echo "      確かめる:"
      echo "        ollama ps                     # 何が載っているか・いつ降りるか"
      echo "        他のツールを止めてから、もう一度実行する"
      echo "      設定を見る（この 2 つは Ollama 側の設定です）:"
      echo "        OLLAMA_MAX_LOADED_MODELS / OLLAMA_NUM_PARALLEL"
    else
      ok "${WANT} だけが読み込まれています"
    fi
  fi
fi


# ---------------------------------------------------------- 3. Docker かどうか
say "4. アプリはどこで動いているか"
IN_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx biomni-app; then
  IN_DOCKER=1
  ok "Docker コンテナ biomni-app が動いています"
else
  warn "コンテナ biomni-app は動いていません（bash scripts/start.sh で直接起動していると判断します）"
fi

if [[ $IN_DOCKER -eq 0 ]]; then
  # 直接起動: localhost で届くならそれでよい
  if [[ "$CONFIGURED" == *localhost* || "$CONFIGURED" == *127.0.0.1* ]]; then
    ok "直接起動なら ${CONFIGURED} で問題ありません"
    echo
    echo "  それでも未接続と出る場合は、アプリを起動し直してください。"
    echo "  .env はプロセス起動時に 1 度だけ読まれます。"
  else
    ng "直接起動なのに ${CONFIGURED} を見に行く設定になっています"
    echo "      .env の OLLAMA_BASE_URL を http://localhost:${PORT} に戻してください"
    echo "      （そのあとアプリを起動し直してください）"
    exit 1
  fi
  exit 0
fi

# --------------------------------------------- 5. コンテナの中から到達するか
say "5. コンテナの中から Ollama が見えるか"
echo "  コンテナの中の localhost は「コンテナ自身」で、ホストではありません。"
echo
FOUND=""
for url in "http://host.docker.internal:${PORT}" "http://ollama:${PORT}" "$CONFIGURED"; do
  r=$(probe_in_container "$url")
  if [[ "$r" == OK ]]; then ok "$url  到達"; [[ -z "$FOUND" ]] && FOUND="$url"
  else ng "$url  到達できず"; fi
done

say "結論"
if [[ -z "$FOUND" ]]; then
  ng "コンテナからはどの経路でも Ollama に届きません"
  echo
  echo "  macOS / Windows で、ホストに直接 Ollama を入れている場合:"
  echo "    Ollama は既定で 127.0.0.1 だけを待ち受けるため、コンテナから届きません。"
  echo "    すべてのインタフェースで待ち受けさせてください。"
  echo
  echo "      macOS:    launchctl setenv OLLAMA_HOST \"0.0.0.0\""
  echo "                （そのあと Ollama.app を終了して起動し直す）"
  echo "      Linux:    systemctl edit ollama    ->  Environment=\"OLLAMA_HOST=0.0.0.0\""
  echo "                sudo systemctl restart ollama"
  echo
  echo "    そのあと:  bash scripts/set-provider.sh ollama && make update"
  echo
  echo "  ホストの Ollama を使わず、コンテナ版で完結させる場合:"
  echo "      bash scripts/set-provider.sh ollama && make update"
  exit 1
fi

if [[ "$CONFIGURED" == "$FOUND" ]]; then
  ok "設定は正しいです（${FOUND}）"
  echo
  echo "  それでも未接続と出るなら、コンテナが古い設定のままです:"
  echo "      make update"
else
  ng "設定が ${CONFIGURED} ですが、実際に届くのは ${FOUND} です"
  echo
  echo "  直す:"
  if [[ "$FOUND" == *host.docker.internal* ]]; then
    echo "      bash scripts/set-provider.sh ollama"
  else
    echo "      .env に  OLLAMA_BASE_URL=${FOUND}  と書く"
  fi
  echo "      make update"
fi
