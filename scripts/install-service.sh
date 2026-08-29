#!/usr/bin/env bash
# Linux に常設する（Docker + systemd）.
#
#   bash scripts/install-service.sh              # システムサービスとして導入
#   bash scripts/install-service.sh --user       # ユーザーサービス（sudo 不要）
#   bash scripts/install-service.sh --no-start   # 導入だけして起動しない
#
# やること:
#   1. Docker / compose の確認
#   2. .env の用意（UID/GID をホストに合わせる ← bind マウントの権限対策）
#   3. data / workspace の作成
#   4. systemd ユニットの設置と有効化
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
# 非ログインシェルでは $USER が無いことがある
WHO="${USER:-$(id -un)}"
GRP="$(id -gn)"

MODE=system
START=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE=user; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; }
ng()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$1"; }

say "前提の確認"
DOCKER=$(command -v docker || true)
[[ -z "$DOCKER" ]] && { ng "docker が見つかりません → https://docs.docker.com/engine/install/"; exit 1; }
ok "docker: $DOCKER"
if ! docker compose version >/dev/null 2>&1; then
  ng "docker compose (v2) が使えません → docker-compose-plugin を入れてください"; exit 1
fi
ok "docker compose: $(docker compose version --short 2>/dev/null)"
if ! docker info >/dev/null 2>&1; then
  ng "docker デーモンに接続できません"
  echo "      sudo systemctl enable --now docker"
  echo "      sudo usermod -aG docker $WHO   （そのあと再ログイン）"
  exit 1
fi
ok "docker デーモンに接続できました"
if ! systemctl --version >/dev/null 2>&1; then
  ng "systemd がありません。make docker-up で直接起動してください"; exit 1
fi

say "設定ファイル (.env)"
[[ -f .env ]] || { cp .env.example .env; ok ".env を作成しました"; }
# コンテナをホストと同じ UID/GID で動かす。ずれると ./data と ./workspace に書けない
set_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    sed -i "s#^${key}=.*#${key}=${value}#" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
set_env APP_UID "$(id -u)"
set_env APP_GID "$(id -g)"
ok "APP_UID=$(id -u) / APP_GID=$(id -g) を .env に設定しました"
if grep -q '^ANTHROPIC_API_KEY=sk-' .env 2>/dev/null; then
  ok "ANTHROPIC_API_KEY 設定済み（Claude API が使えます）"
else
  warn "ANTHROPIC_API_KEY 未設定。Ollama のみで動きます"
  echo "      Claude も使うなら .env に  ANTHROPIC_API_KEY=sk-ant-...  を追記"
fi
MODEL=$(sed -n 's/^HYPO_MODEL=//p' .env | head -1)
ok "使うモデル: ${MODEL:-qwen3:14b}"
PORT=$(sed -n 's/^APP_PORT=//p' .env | head -1); PORT="${PORT:-5002}"
BIND=$(sed -n 's/^APP_BIND=//p' .env | head -1); BIND="${BIND:-0.0.0.0}"
ok "待ち受け: ${BIND}:${PORT}  （.env の APP_PORT / APP_BIND で変えられます）"

say "データ用ディレクトリ"
mkdir -p data workspace
ok "$REPO/data と $REPO/workspace"

say "systemd ユニット"
if [[ "$MODE" == system ]] && ! command -v sudo >/dev/null 2>&1 && [[ "$(id -u)" != 0 ]]; then
  ng "sudo がありません。--user を付けてユーザーサービスとして導入してください"
  echo "      bash scripts/install-service.sh --user"
  exit 1
fi
UNIT=biomni-hypo.service
TMP=$(mktemp)
sed -e "s#__WORKDIR__#${REPO}#g" -e "s#__DOCKER__#${DOCKER}#g" deploy/"$UNIT" > "$TMP"
sed -i -e "s#^User=__USER__#User=${WHO}#" -e "s#^Group=__USER__#Group=${GRP}#" "$TMP"

if [[ "$MODE" == user ]]; then
  # ユーザーサービス。sudo 不要だが、ログインしていなくても動かすには linger が要る。
  # ユーザーユニットからシステムユニット（docker.service）へ Requires= はできないので外す
  sed -i '/^User=/d; /^Group=/d; /^WantedBy=/d' "$TMP"
  sed -i '/^Requires=docker.service/d; /^After=docker.service/d; /^Wants=network-online.target/d' "$TMP"
  printf 'WantedBy=default.target\n' >> "$TMP"
  mkdir -p "$HOME/.config/systemd/user"
  install -m 644 "$TMP" "$HOME/.config/systemd/user/$UNIT"
  systemctl --user daemon-reload
  ok "$HOME/.config/systemd/user/$UNIT"
  if loginctl enable-linger "$WHO" 2>/dev/null; then
    ok "linger を有効化（ログアウトしても動き続けます）"
  else
    warn "linger を有効化できませんでした。ログアウトすると止まります"
    echo "      sudo loginctl enable-linger $WHO"
  fi
  CTL="systemctl --user"
else
  sudo install -m 644 "$TMP" "/etc/systemd/system/$UNIT" || { ng "設置に失敗（sudo が必要です）"; exit 1; }
  sudo systemctl daemon-reload
  ok "/etc/systemd/system/$UNIT"
  CTL="sudo systemctl"
fi
rm -f "$TMP"

if $CTL enable "$UNIT" >/dev/null 2>&1; then
  ok "自動起動を有効化（再起動後も復帰します）"
else
  warn "自動起動を有効化できませんでした:  $CTL enable $UNIT"
fi

if [[ $START -eq 1 ]]; then
  say "起動"
  echo "  初回はイメージのビルドとモデル取得で時間がかかります（数分〜数十分）"
  $CTL start "$UNIT" || { ng "起動に失敗しました"; echo "      $CTL status $UNIT"; exit 1; }
  ok "起動しました"
fi

say "できあがり"
cat <<MSG
  http://localhost:${PORT}

  状態      : $CTL status $UNIT
  ログ      : $CTL -o cat -f -u $UNIT      （コンテナのログは docker compose logs -f app）
  再起動    : $CTL restart $UNIT
  停止      : $CTL stop $UNIT
  自動起動を切る : $CTL disable $UNIT
  更新      : git pull && $CTL reload $UNIT   （--build して入れ替え）
  取り外し  : bash scripts/uninstall-service.sh $([[ "$MODE" == user ]] && echo --user)

  初回のモデル取得の進行:
      docker compose logs -f ollama-pull
MSG
