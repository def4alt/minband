#!/usr/bin/env bash
# OS-level impairment of the UDP ingest port using macOS dummynet. For honest measurements;
# the in-process shaper in the server is what the stage demo uses.
#
#   sudo tools/link.sh set 8Kbit/s 150ms 0.05    # bandwidth, delay, loss
#   sudo tools/link.sh clear
set -euo pipefail
PORT="${MINBAND_PORT:-7777}"
PIPE=7
case "${1:-}" in
  set)
    BW="${2:-16Kbit/s}"; DELAY="${3:-100ms}"; PLR="${4:-0}"
    dnctl pipe $PIPE config bw "$BW" delay "${DELAY%ms}" plr "$PLR"
    ANCHOR=$(mktemp)
    printf 'dummynet in proto udp from any to any port %s pipe %s\n' "$PORT" "$PIPE" > "$ANCHOR"
    pfctl -q -a minband -f "$ANCHOR"
    pfctl -q -e 2>/dev/null || true
    rm -f "$ANCHOR"
    echo "udp :$PORT -> pipe $PIPE: bw=$BW delay=$DELAY plr=$PLR"
    ;;
  clear)
    pfctl -q -a minband -F all 2>/dev/null || true
    dnctl pipe delete $PIPE 2>/dev/null || true
    echo "cleared"
    ;;
  *) echo "usage: $0 set <bw> <delay> <plr> | clear"; exit 1;;
esac
