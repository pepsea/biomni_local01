#!/usr/bin/env bash
# 「Ollama は起動しているのにアプリからは未接続」を切り分ける.
#
#   bash scripts/diagnose-ollama.sh
#
# 見るのは 3 か所。どこで切れているかで対処が変わる。
#   1. ホストから Ollama            -> Ollama 自体が動いているか
#   2. アプリの設定 (.env)          -> どの URL を見に行くか
#   3. コンテナから Ollama          -> Docker の場合。localhost はコンテナ自身を指す
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

PORT=$(sed -n 's/^OLLAMA_PORT=//p' .env 2>/dev/null | head -1); PORT="${PORT:-11434}"
CONFIGURED=$(sed -n 's/^OLLAMA_BASE_URL=//p' .env 2>/dev/null | head -1)
CONFIGURED="${CONFIGURED:-http://localhost:11434}"

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

# ---------------------------------------------------------- 3. Docker かどうか
say "3. アプリはどこで動いているか"
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

# --------------------------------------------- 4. コンテナの中から到達するか
say "4. コンテナの中から Ollama が見えるか"
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
  echo "    そのあと:  bash scripts/set-provider.sh ollama && make docker-rebuild"
  echo
  echo "  ホストの Ollama を使わず、コンテナ版で完結させる場合:"
  echo "      bash scripts/set-provider.sh ollama && make docker-rebuild"
  exit 1
fi

if [[ "$CONFIGURED" == "$FOUND" ]]; then
  ok "設定は正しいです（${FOUND}）"
  echo
  echo "  それでも未接続と出るなら、コンテナが古い設定のままです:"
  echo "      make docker-rebuild"
else
  ng "設定が ${CONFIGURED} ですが、実際に届くのは ${FOUND} です"
  echo
  echo "  直す:"
  if [[ "$FOUND" == *host.docker.internal* ]]; then
    echo "      bash scripts/set-provider.sh ollama"
  else
    echo "      .env に  OLLAMA_BASE_URL=${FOUND}  と書く"
  fi
  echo "      make docker-rebuild"
fi
