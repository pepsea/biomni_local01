#!/usr/bin/env bash
# docker compose up の前に、ポートの衝突と設定の食い違いを見る。
#
#   bash scripts/docker-preflight.sh
#
# 「ports are not available: ... address already in use」を、
# 起動を試みる前に、原因つきで出すためのもの。
set -uo pipefail
cd "$(dirname "$0")/.."

ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

env_of() { sed -n "s/^$1=//p" .env 2>/dev/null | head -1; }

APP_PORT=$(env_of APP_PORT);       APP_PORT="${APP_PORT:-8000}"
OLLAMA_PORT=$(env_of OLLAMA_PORT); OLLAMA_PORT="${OLLAMA_PORT:-11434}"
PROFILES=$(env_of COMPOSE_PROFILES)
PROVIDER=$(env_of HYPO_PROVIDER);  PROVIDER="${PROVIDER:-ollama}"
OLLAMA_URL=$(env_of OLLAMA_BASE_URL)

# そのポートを誰が使っているか
who_has() {
  local port="$1" out=""
  if command -v ss >/dev/null 2>&1; then
    out=$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p"$" {print $NF}' | head -1)
  fi
  if [[ -z "$out" ]] && command -v lsof >/dev/null 2>&1; then
    out=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" (pid "$2")"}')
  fi
  printf '%s' "$out"
}
in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk -v p=":$port" '$4 ~ p"$" {found=1} END{exit !found}' && return 0
  fi
  command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  return 1
}
# そのポートが 0.0.0.0（全インタフェース）で待ち受けているか。
# 127.0.0.1 だけだと host.docker.internal 経由でコンテナから届かない（§17.4）
listens_on_all() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -qE "(0\.0\.0\.0|\*|\[::\]):$1\b"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -qE '\*:'
  else
    return 0   # 判定できないときは警告しない
  fi
}

# コンテナの中から実際に URL を叩いてみる。
#
# 待ち受けアドレス（127.0.0.1 か 0.0.0.0 か）から推測すると外す。
# Docker Desktop（macOS / Windows）は host.docker.internal からホストの
# ループバックへ転送できることがあり、Linux の host-gateway は届かない。
# 環境ごとの正解を覚えるより、その場で試すほうが確実で短い。
#
#   0 = 届いた / 1 = 届かなかった / 2 = 試せなかった（イメージ未ビルドなど）
probe_from_container() {
  local url="$1"
  command -v docker >/dev/null 2>&1 || return 2
  docker compose images app 2>/dev/null | grep -q . || return 2   # 未ビルド
  if docker compose run --rm --no-deps --entrypoint sh app \
       -c "curl -sf -m 5 '${url}/api/tags' >/dev/null" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# その名前のコンテナが自分のものか（= compose が再利用するので衝突しない）
ours() { docker compose ps --format '{{.Service}}' 2>/dev/null | grep -qx "$1"; }

# Ollama を全インタフェースで待ち受けさせる手順。
# ポートを必ず添えること。OLLAMA_HOST=0.0.0.0 だけ書くと既定の 11434 に戻り、
# 別ポートで動かしている場合は設定を壊す
ollama_host_hint() {
  local port="$1"
  cat <<MSG
      ホストの Ollama を全インタフェースで待ち受けさせてください。
      ポート（${port}）を必ず付けること。付けないと 11434 に戻ります。

        Linux : sudo systemctl edit ollama
                  [Service]
                  Environment="OLLAMA_HOST=0.0.0.0:${port}"
                sudo systemctl restart ollama
        macOS : launchctl setenv OLLAMA_HOST "0.0.0.0:${port}"
                （そのあと Ollama.app を終了して起動し直す）

      設定後の確認:
        curl -s http://localhost:${port}/api/tags >/dev/null && echo OK
        make docker-rebuild        （このチェックがもう一度走ります）
MSG
}

printf '\n\033[1m== 起動前チェック\033[0m\n'

# 何より先に Docker が動いているか。動いていなければ、この先の確認は
# すべて無意味（compose も docker exec も使えない）
if ! command -v docker >/dev/null 2>&1; then
  ng "docker が見つかりません → https://docs.docker.com/get-docker/"
  echo
  echo "  Docker を使わずに動かせます（Ollama はホストにあるので、そのほうが単純です）:"
  echo "      bash scripts/start.sh"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  ng "Docker デーモンに接続できません。Docker が起動していません"
  echo "      macOS  : Docker Desktop を起動してください（メニューバーのクジラ）"
  echo "               open -a Docker    # そのあと 30 秒ほど待つ"
  echo "      Linux  : sudo systemctl start docker"
  echo
  echo "  Docker を使わずに動かせます（Ollama はホストにあるので、そのほうが単純です）:"
  echo "      bash scripts/start.sh"
  exit 1
fi

printf '  provider=%s / COMPOSE_PROFILES=%s / APP_PORT=%s\n' \
       "$PROVIDER" "${PROFILES:-（空）}" "$APP_PORT"
printf '  OLLAMA_BASE_URL=%s\n' "${OLLAMA_URL:-（未設定）}"

FAIL=0

# --- アプリのポート ---
if in_use "$APP_PORT" && ! ours app; then
  ng "APP_PORT=$APP_PORT は既に使われています： $(who_has "$APP_PORT")"
  echo "      .env の APP_PORT を空いている番号に変えてください（例: APP_PORT=8003）"
  FAIL=1
else
  ok "APP_PORT=$APP_PORT は使えます"
fi

# --- ollama コンテナ（もう compose には無い）---
# COMPOSE_PROFILES=ollama が .env に残っていることがある。.env は git 管理外
# なので、pull しても古い値が残り続ける。profile 名だけ残っていても実体が
# 無いので害は無いが、紛らわしいので指摘する
if [[ ",$PROFILES," == *",ollama,"* ]]; then
  warn "COMPOSE_PROFILES=ollama が .env に残っています（この profile はもうありません）"
  echo "      Ollama はホストのものだけを使う構成です（docs/design/21 §21.16）。"
  echo "      揃える:  bash scripts/set-provider.sh ollama"
fi

if true; then
  ok "ollama コンテナは起動しません（ホストの Ollama を使う構成）"

  # profiles を外しても、既に動いているコンテナは compose が止めない。
  # しかも restart: unless-stopped なので再起動しても戻ってくる。
  # 残ったまま（中身は空）だと、アプリがそちらを掴んで
  # 「モデルが全部 未取得」になる（docs/design/21 §21.15）。
  if command -v docker >/dev/null 2>&1 \
     && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx biomni-ollama; then
    ng "ollama コンテナ biomni-ollama が動いたままです"
    docker ps --format '      {{.Names}}  {{.Ports}}  ({{.Status}})' 2>/dev/null \
      | grep biomni-ollama
    echo "      COMPOSE_PROFILES は空ですが、compose は既に動いているものを止めません。"
    echo "      restart: unless-stopped なので、再起動しても戻ってきます。"
    echo
    echo "      消す:  make docker-stop-ollama"
    echo "             （コンテナだけ消えます。モデルはボリュームに残ります）"
    FAIL=1
  fi
  # Ollama を使わない構成（Claude のみ）なら、ここは関係しない。
  # provider=ollama なら起動しても仕事にならないので止める。
  # そうでなければ「Ollama のモデルは選べない」という警告に留める
  if [[ "$PROVIDER" == ollama ]]; then BLOCKING=1; else BLOCKING=0; fi
  if [[ "$PROVIDER" == ollama || -n "$OLLAMA_URL" ]]; then
    if [[ "$OLLAMA_URL" != *host.docker.internal* ]]; then
      if [[ $BLOCKING -eq 1 ]]; then
        ng "OLLAMA_BASE_URL がホストを指していません: ${OLLAMA_URL:-（未設定）}"
        FAIL=1
      else
        warn "OLLAMA_BASE_URL がホストを指していません: ${OLLAMA_URL:-（未設定）}"
        echo "      Claude は使えますが、Ollama のモデルは選べません。"
      fi
      echo "      コンテナの中の localhost はコンテナ自身です。ホストには届きません。"
      echo "      直す:  bash scripts/set-provider.sh ollama    （both でも可）"
    else
      ok "ホストの Ollama を使う設定です: ${OLLAMA_URL}"
      # URL のポートと、実際に Ollama が待っているポートが食い違っていないか。
      # コンテナ版をやめたので OLLAMA_PORT は「ホストの Ollama のポート」になった。
      URL_PORT="${OLLAMA_URL##*:}"; URL_PORT="${URL_PORT%%/*}"
      if ! curl -sf -m 3 "http://localhost:${URL_PORT}/api/tags" >/dev/null 2>&1; then
        if [[ $BLOCKING -eq 1 ]]; then ng "ホストの localhost:${URL_PORT} に Ollama がいません"
        else warn "ホストの localhost:${URL_PORT} に Ollama がいません（Claude のみ使えます）"; fi
        if curl -sf -m 3 "http://localhost:11434/api/tags" >/dev/null 2>&1; then
          echo "      11434 では応答しています。.env の OLLAMA_PORT を 11434 に直してください:"
          echo "          bash scripts/set-provider.sh ollama    （OLLAMA_BASE_URL も揃います）"
        else
          echo "      ollama serve  を起動してください"
        fi
        [[ $BLOCKING -eq 1 ]] && FAIL=1
      else
        # ホストからは届いた。次は「コンテナから届くか」。
        # 待ち受けアドレスからの推測ではなく、実際に叩いて確かめる
        probe_from_container "$OLLAMA_URL"
        case $? in
          0) ok "コンテナからホストの Ollama に到達できました（実測）" ;;
          2)
            if listens_on_all "$URL_PORT"; then
              ok "ホストの Ollama は全インタフェースで待ち受けています"
            else
              warn "Ollama が 127.0.0.1 だけを待ち受けています"
              echo "      コンテナから届かない可能性があります。"
              echo "      （イメージが未ビルドのため実測できませんでした。"
              echo "        ビルド後にもう一度このチェックが走ります）"
              ollama_host_hint "$URL_PORT"
            fi
            ;;
          *)
            if [[ $BLOCKING -eq 1 ]]; then
              ng "コンテナからホストの Ollama に届きません（実測）"
              FAIL=1
            else
              warn "コンテナからホストの Ollama に届きません（実測）"
              echo "      Claude は使えますが、Ollama のモデルは選べません。"
            fi
            ollama_host_hint "$URL_PORT"
            ;;
        esac
      fi
    fi
  fi
fi

# --- Claude API ---
if [[ "$PROVIDER" == anthropic ]]; then
  if grep -qE '^ANTHROPIC_API_KEY=sk-' .env 2>/dev/null; then
    ok "ANTHROPIC_API_KEY 設定済み"
  else
    ng "provider=anthropic ですが ANTHROPIC_API_KEY が未設定です"
    echo "      bash scripts/set-provider.sh claude --key sk-ant-..."
    FAIL=1
  fi
fi

if [[ $FAIL -eq 1 ]]; then
  printf '\n  \033[31m上の点を解消してから起動してください。\033[0m\n\n'
  exit 1
fi
printf '\n'
