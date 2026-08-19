#!/usr/bin/env bash
# 常設を取り外す。データ（./data, ./workspace）とモデルは消さない。
#
#   bash scripts/uninstall-service.sh            # システムサービスを削除
#   bash scripts/uninstall-service.sh --user     # ユーザーサービスを削除
#   bash scripts/uninstall-service.sh --purge    # モデルのボリュームも削除
set -uo pipefail
cd "$(dirname "$0")/.."

MODE=system; PURGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE=user; shift ;;
    --purge) PURGE=1; shift ;;
    *) echo "不明なオプション: $1" >&2; exit 1 ;;
  esac
done

UNIT=biomni-hypo.service
SUDO=""; [[ "$(id -u)" != 0 ]] && command -v sudo >/dev/null 2>&1 && SUDO=sudo
if [[ "$MODE" == user ]]; then
  CTL="systemctl --user"; UNIT_PATH="$HOME/.config/systemd/user/$UNIT"
else
  CTL="$SUDO systemctl"; UNIT_PATH="/etc/systemd/system/$UNIT"
fi

$CTL stop "$UNIT" 2>/dev/null
$CTL disable "$UNIT" 2>/dev/null
if [[ "$MODE" == user ]]; then rm -f "$UNIT_PATH"; else $SUDO rm -f "$UNIT_PATH"; fi
$CTL daemon-reload
echo "✓ サービスを削除しました"

if [[ $PURGE -eq 1 ]]; then
  docker compose down -v
  echo "✓ コンテナとモデルのボリュームを削除しました"
else
  docker compose down 2>/dev/null
  echo "✓ コンテナを停止しました（モデルと ./data ./workspace は残しています）"
fi
