#!/usr/bin/env bash
# 非 Docker の常駐を取り外す（data / workspace は消さない）.
set -uo pipefail
cd "$(dirname "$0")/.."
ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; }
LABEL=com.biomni-hypo.app
UNIT=biomni-hypo

printf '\n\033[1m== 取り外す\033[0m\n'
case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
    [[ -f "$PLIST" ]] && launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST" && ok "削除: $PLIST"
    ;;
  Linux)
    systemctl --user disable --now "${UNIT}.service" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/${UNIT}.service"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "削除: ~/.config/systemd/user/${UNIT}.service"
    ;;
esac
printf '\n  data / workspace / logs は残しています。\n\n'
