#!/usr/bin/env bash
# 最新を取り込んで、動いているものを入れ替える.
#
#   bash scripts/update.sh              # git pull して、動いている形に合わせて再起動
#   bash scripts/update.sh --no-pull    # 取り込み済み。再起動だけ
#
# 常駐のさせ方が 3 通り（systemd ユーザー / launchd / Docker）あり、
# どれで動かしているかで再起動のコマンドが違う。毎回それを覚える必要が
# 無いように、動いているものを見て選ぶ。使っていない方式には触らない。
set -uo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0
[[ "${1:-}" =~ ^(-h|--help)$ ]] && { sed -n '2,12p' "$0"; exit 0; }

UNIT=biomni-hypo
LABEL=com.biomni-hypo.app
PORT=$(sed -n 's/^APP_PORT=//p' .env 2>/dev/null | head -1); PORT="${PORT:-5002}"

if [[ $PULL -eq 1 ]]; then
  say "取り込み"
  if git pull --ff-only; then ok "git pull"; else ng "git pull に失敗しました"; exit 1; fi
fi

say "動いているものを探す"

running_docker() {
  command -v docker >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1 || return 1
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx biomni-app
}
running_systemd_user() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user is-active "${UNIT}.service" >/dev/null 2>&1
}
running_systemd_system() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl is-active "${UNIT}.service" >/dev/null 2>&1
}
running_launchd() {
  command -v launchctl >/dev/null 2>&1 || return 1
  launchctl list 2>/dev/null | grep -q "${LABEL}"
}

MODE=""
if   running_systemd_user;   then MODE="systemd-user"
elif running_systemd_system; then MODE="systemd-system"
elif running_launchd;        then MODE="launchd"
elif running_docker;         then MODE="docker"
fi

case "$MODE" in
  systemd-user)
    ok "systemd（ユーザー）で常駐しています"
    systemctl --user restart "${UNIT}.service" && ok "再起動しました" || { ng "再起動に失敗"; exit 1; }
    ;;
  systemd-system)
    ok "systemd（システム）で常駐しています"
    echo "      root 権限が要ります:"
    echo "          sudo systemctl restart ${UNIT}"
    exit 0
    ;;
  launchd)
    ok "launchd で常駐しています"
    launchctl unload "$HOME/Library/LaunchAgents/${LABEL}.plist" 2>/dev/null
    launchctl load   "$HOME/Library/LaunchAgents/${LABEL}.plist" 2>/dev/null && ok "再起動しました"
    ;;
  docker)
    ok "Docker で動いています"
    docker compose up -d --build app || { ng "再作成に失敗しました"; exit 1; }
    ok "作り直しました"
    ;;
  *)
    # ここが「Docker デーモンに接続できません」で止まっていた場所。
    # 使っていない方式のエラーを出さないこと。動いていないなら、そう言う。
    warn "常駐していません（systemd / launchd / Docker のどれでも動いていません）"
    echo
    echo "      その場で起動する:"
    echo "          bash scripts/start.sh"
    echo "      常駐させる（Docker 不要）:"
    echo "          bash scripts/install-local-service.sh"
    exit 0
    ;;
esac

say "確認"
for _ in $(seq 1 20); do
  BUILD=$(curl -sf -m 2 "http://localhost:${PORT}/api/health" 2>/dev/null \
          | sed -n 's/.*"build"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [[ -n "$BUILD" ]] && break
  sleep 1
done
if [[ -n "$BUILD" ]]; then
  ok "http://localhost:${PORT} が応答しました（build ${BUILD}）"
  HEAD=$(git rev-parse --short HEAD 2>/dev/null)
  if [[ -n "$HEAD" && "$BUILD" != "$HEAD" ]]; then
    warn "画面のビルド ${BUILD} が手元の ${HEAD} と違います。古いプロセスが残っていませんか"
    echo "          ss -ltnp | grep :${PORT}"
  fi
else
  ng "応答がありません"
  echo "      ログ: tail -20 logs/app.log"
fi
