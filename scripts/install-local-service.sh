#!/usr/bin/env bash
# Docker を使わずに常駐させる（ホストの Ollama をそのまま使う構成）.
#
#   bash scripts/install-local-service.sh              # 常駐させる
#   bash scripts/install-local-service.sh --no-start   # 設置だけ
#   bash scripts/uninstall-local-service.sh            # 取り外す
#
# macOS  -> launchd の LaunchAgent（~/Library/LaunchAgents）
# Linux  -> systemd のユーザーユニット（~/.config/systemd/user）
#
# どちらもログイン中のユーザーとして動く。root は要らない。
# Ollama がホストで動いているので、アプリも同じホストで動かせば
# localhost にそのまま届く（docs/design/21 §21.19）。
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

START=1
[[ "${1:-}" == "--no-start" ]] && START=0
[[ "${1:-}" =~ ^(-h|--help)$ ]] && { sed -n '2,13p' "$0"; exit 0; }

LABEL=com.biomni-hypo.app
UNIT=biomni-hypo

# ---------------------------------------------------------------- 前提確認
say "前提の確認"

PY=""
for candidate in "$REPO/.venv/bin/python" "${VIRTUAL_ENV:-}/bin/python"; do
  [[ -n "$candidate" && -x "$candidate" ]] && { PY="$candidate"; break; }
done
if [[ -z "$PY" ]]; then
  ng "仮想環境が見つかりません"
  echo "      bash scripts/setup_local.sh   を先に実行してください"
  exit 1
fi
ok "Python: $PY"

if ! "$PY" -c "import uvicorn, biomni_hypo" 2>/dev/null; then
  ng "依存が足りません（uvicorn / biomni_hypo を import できません）"
  echo "      bash scripts/setup_local.sh"
  exit 1
fi
ok "依存 OK"

[[ -f .env ]] || { cp .env.example .env; ok ".env を作成しました"; }
PORT=$(sed -n 's/^APP_PORT=//p' .env | head -1); PORT="${PORT:-5002}"
BIND=$(sed -n 's/^APP_BIND=//p' .env | head -1); BIND="${BIND:-127.0.0.1}"
OLLAMA_URL=$(sed -n 's/^OLLAMA_BASE_URL=//p' .env | head -1)
ok "待ち受け: ${BIND}:${PORT}"

# Docker を使わないので、コンテナ向けの名前が残っていると届かない
if [[ "$OLLAMA_URL" == *host.docker.internal* ]]; then
  ng "OLLAMA_BASE_URL が host.docker.internal を指しています: $OLLAMA_URL"
  echo "      Docker を使わない構成では localhost です。直す:"
  echo "          bash scripts/set-provider.sh ollama"
  exit 1
fi
ok "OLLAMA_BASE_URL=${OLLAMA_URL:-（未設定。既定の localhost:11434 を使います）}"

mkdir -p "$REPO/logs" "$REPO/data" "$REPO/workspace"
LOG="$REPO/logs/app.log"

# ---------------------------------------------------------------- 保存先
# 常駐させると、保存先が開けない状態は「毎回警告が出る」になる。設置の時点で
# 実際に sqlite を開いて書いてみて、駄目ならローカルディスクに寄せ、その値を
# unit に焼き込む。リポジトリがネットワークマウント上にあると開けない
# （docs/design/27）。開くだけでなく書くこと。NFS は COMMIT で落ちる。
probe_workspace() {
  "$PY" -c 'import sys; from backend.app.store import probe_workspace; sys.stdout.write(probe_workspace(sys.argv[1]))' "$1"
}

WORKSPACE=$(sed -n 's/^HYPO_WORKSPACE=//p' .env | head -1)
WORKSPACE="${WORKSPACE:-$REPO/workspace}"
WHY=$(probe_workspace "${WORKSPACE}")
if [[ -z "$WHY" ]]; then
  ok "保存先: ${WORKSPACE}"
else
  STATE="${XDG_STATE_HOME:-$HOME/.local/state}/biomni-hypo/workspace"
  warn "保存先 ${WORKSPACE} は使えません"
  echo "      ${WHY}"
  WHY2=$(probe_workspace "${STATE}")
  if [[ -z "$WHY2" ]]; then
    WORKSPACE="$STATE"
    ok "保存先: ${WORKSPACE}（ローカルディスクに寄せました）"
  else
    ng "保存先をどこにも作れません"
    echo "      ${STATE}: ${WHY2}"
    echo "      .env の HYPO_WORKSPACE に書ける場所を指定してください。"
    exit 1
  fi
fi

case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux)  OS=linux ;;
  *) ng "未対応の OS です: $(uname -s)"; exit 1 ;;
esac

# ------------------------------------------------------------------ 設置
if [[ "$OS" == macos ]]; then
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  say "launchd に設置する"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>-m</string><string>uvicorn</string>
    <string>backend.app.main:app</string>
    <string>--host</string><string>${BIND}</string>
    <string>--port</string><string>${PORT}</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HYPO_WORKSPACE</key><string>${WORKSPACE}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST_EOF
  # 壊れた plist を置いたまま気付かない、を防ぐ
  if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$PLIST" >/dev/null 2>&1; then
    ng "plist が壊れています: $PLIST"; exit 1
  fi
  ok "設置: $PLIST"

  if [[ $START -eq 1 ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    if launchctl load "$PLIST" 2>/dev/null; then
      ok "起動しました"
    else
      ng "launchctl load に失敗しました"; exit 1
    fi
  fi
  CTL_STATUS="launchctl list | grep ${LABEL}"
  CTL_STOP="launchctl unload ~/Library/LaunchAgents/${LABEL}.plist"
  CTL_START="launchctl load ~/Library/LaunchAgents/${LABEL}.plist"
else
  SERVICE="$HOME/.config/systemd/user/${UNIT}.service"
  say "systemd（ユーザー）に設置する"
  command -v systemctl >/dev/null 2>&1 || { ng "systemd がありません"; exit 1; }
  mkdir -p "$(dirname "$SERVICE")"
  cat > "$SERVICE" <<UNIT_EOF
[Unit]
Description=Biomni 仮説構築アプリ（Docker 無し・ホストの Ollama を使用）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO}
Environment=HYPO_WORKSPACE=${WORKSPACE}
ExecStart=${PY} -m uvicorn backend.app.main:app --host ${BIND} --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=append:${LOG}
StandardError=append:${LOG}

[Install]
WantedBy=default.target
UNIT_EOF
  ok "設置: $SERVICE"
  # ユーザーセッション（D-Bus）が無いと systemctl --user は使えない。
  # ヘッドレスのサーバや、SSH で入っただけの環境で普通に起こる。
  # 「起動に失敗」とだけ言われても打つ手が分からないので、理由と代替を出す
  if ! systemctl --user daemon-reload 2>/dev/null; then
    ng "systemctl --user が使えません（ユーザーセッションがありません）"
    echo "      unit は設置済みです: $SERVICE"
    echo
    echo "      次のどれかで動かせます:"
    echo "        1) ログイン後に常駐させる（linger を有効化してから再実行）"
    echo "             sudo loginctl enable-linger $(id -un)"
    echo "             bash scripts/install-local-service.sh"
    echo "        2) システム全体のサービスにする（root 権限が要ります）"
    echo "             sudo cp $SERVICE /etc/systemd/system/${UNIT}.service"
    echo "             sudo systemctl daemon-reload && sudo systemctl enable --now ${UNIT}"
    echo "        3) 常駐させず、その場で動かす"
    echo "             bash scripts/start.sh"
    exit 1
  fi
  if [[ $START -eq 1 ]]; then
    if systemctl --user enable --now "${UNIT}.service" 2>/dev/null; then
      ok "起動しました"
    else
      ng "起動に失敗しました"
      systemctl --user status "${UNIT}.service" --no-pager 2>&1 | head -12 | sed 's/^/      /'
      exit 1
    fi
  fi
  if ! loginctl show-user "$(id -un)" 2>/dev/null | grep -q "Linger=yes"; then
    warn "ログアウトすると止まります。ログイン前から動かすなら:"
    echo "      sudo loginctl enable-linger $(id -un)"
  fi
  CTL_STATUS="systemctl --user status ${UNIT}"
  CTL_STOP="systemctl --user stop ${UNIT}"
  CTL_START="systemctl --user start ${UNIT}"
fi

# ------------------------------------------------------------------ 確認
if [[ $START -eq 1 ]]; then
  say "起動確認"
  for _ in $(seq 1 20); do
    if curl -sf -m 2 "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
      ok "http://localhost:${PORT} が応答しました"
      break
    fi
    sleep 1
  done
  if ! curl -sf -m 2 "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
    ng "応答がありません"
    # ログをそのまま貼るだけでは、どれが原因の行なのか分からない。
    # 見覚えのある壊れ方は名指しして、直し方まで出す（実測で踏んだものだけ）
    if grep -q "address already in use\|Address already in use" "$LOG" 2>/dev/null; then
      echo "      原因: ポート ${PORT} を別のものが掴んでいます。"
      echo "        ss -ltnp | grep :${PORT}"
      echo "        kill \$(lsof -t -iTCP:${PORT} -sTCP:LISTEN)"
      echo "      あるいは .env の APP_PORT を空いている番号に変えて、もう一度この設置を実行してください。"
    elif grep -q "ModuleNotFoundError\|ImportError" "$LOG" 2>/dev/null; then
      echo "      原因: 依存が足りません。"
      echo "        bash scripts/setup_local.sh --full"
    elif grep -q "データベースを開けません\|保存先" "$LOG" 2>/dev/null; then
      echo "      原因: 保存先を開けません。設置し直すと書ける場所を選び直します。"
      echo "        bash scripts/install-local-service.sh"
      echo "      場所を指定するなら .env に:  HYPO_WORKSPACE=\$HOME/.biomni-hypo/workspace"
    fi
    echo
    echo "      ログ（${LOG} の末尾）:"
    tail -20 "$LOG" 2>/dev/null | sed 's/^/        /'
    echo
    echo "      サービスの状態:"
    ${CTL_STATUS} --no-pager 2>&1 | head -8 | sed 's/^/        /'
    exit 1
  fi
fi

say "使い方"
cat <<MSG
  開く:     http://localhost:${PORT}
  ログ:     tail -f ${LOG}
  状態:     ${CTL_STATUS}
  停止:     ${CTL_STOP}
  再開:     ${CTL_START}
  取り外す: bash scripts/uninstall-local-service.sh

  コードを更新したら:
      git pull && ${CTL_STOP} && ${CTL_START}
MSG
