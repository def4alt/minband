#!/usr/bin/env bash
# Dry-run golden test for tools/pi-link.sh: the exact tc commands of every profile (rate, delay,
# loss, queue limit, filters, both directions), the budget hints, the contested loop's leaf
# changes, clear, status, setup, environment overrides and argument errors. Plain bash; needs
# neither root nor tc, and never changes the network (every run is --dry-run, state in a temp dir).
#
#   tools/test/pi-link.test.sh
#
# The real-kernel counterpart is tools/test/pi-link-kernel.test.sh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../pi-link.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
unset UP_DEV DOWN_DEV PORT SERVER DRY_RUN SSID PASSWORD BAND COUNTRY ETH_ADDR L2_OVERHEAD \
  CONTESTED_UP CONTESTED_DOWN LEAF_QDISC_OVERRIDE ROOT_QDISC_OVERRIDE
export STATE_DIR="$TMP/state"
# Safety net: should a run ever miss --dry-run, it hits these stubs, not the real network.
mkdir -p "$TMP/bin"
for c in tc nmcli setsid; do
  printf '#!/bin/sh\necho "pi-link.test: real %s called, a run missed --dry-run" >&2\nexit 99\n' "$c" > "$TMP/bin/$c"
  chmod +x "$TMP/bin/$c"
done
export PATH="$TMP/bin:$PATH"

PASS=0 FAIL=0
ok() { PASS=$((PASS + 1)); }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$*" >&2; }
# check <name> <condition...>
check() { local name=$1; shift; if "$@"; then ok; else bad "$name"; fi; }
has_line() { grep -Fxq -- "$2" <<< "$1"; }
lacks() { ! grep -Fq -- "$2" <<< "$1"; }
same() {
  if [[ $2 == "$3" ]]; then ok; else bad "$1"; diff <(printf '%s\n' "$3") <(printf '%s\n' "$2") | sed 's/^/    /' >&2 || true; fi
}

OUT="" ERR="" CODE=0
dry() { CODE=0; OUT="$("$SCRIPT" --dry-run "$@" 2>"$TMP/err")" || CODE=$?; ERR="$(cat "$TMP/err")"; }
tc_lines() { grep '^tc ' <<< "$OUT" || true; }

# ---------------------------------------------------------------- lora, fully literal

dry lora
same "lora: full dry-run output" "$OUT" "$(cat <<'EOF'
tc qdisc del dev eth0 root 2>/dev/null || true
tc qdisc add dev eth0 root handle 1: prio bands 4
tc qdisc add dev eth0 parent 1:4 handle 40: netem rate 2kbit -14 delay 300ms loss 10% limit 4
tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip dport 7777 0xffff flowid 1:4
tc qdisc del dev wlan0 root 2>/dev/null || true
tc qdisc add dev wlan0 root handle 1: prio bands 4
tc qdisc add dev wlan0 parent 1:4 handle 40: netem rate 2kbit -14 delay 300ms loss 10% limit 4
tc filter add dev wlan0 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip sport 7777 0xffff flowid 1:4
# lora (Meshtastic-class LoRa): UDP 7777 up on eth0 egress (dport), down on wlan0 egress (sport): netem rate 2kbit -14 delay 300ms loss 10% limit 4
# Set the edge budget (1500 bit/s), on the laptop:
curl 'http://192.168.77.2:8080/api/budget?bps=1500'
# or, if your server has /api/link:
curl 'http://192.168.77.2:8080/api/link?profile=external&as=lora'
EOF
)"
check "lora: exit 0" test "$CODE" -eq 0
check "lora: nothing on stderr" test -z "$ERR"

# ---------------------------------------------------------------- every profile (HACKATHON_PLAN 3.3)

# Expected tc tree for one device: <dev> <dport|sport> <netem args>.
tree() {
  printf '%s\n' \
    "tc qdisc del dev $1 root 2>/dev/null || true" \
    "tc qdisc add dev $1 root handle 1: prio bands 4" \
    "tc qdisc add dev $1 parent 1:4 handle 40: netem $3" \
    "tc filter add dev $1 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip $2 7777 0xffff flowid 1:4"
}

# profile | budget (- = leave it) | netem arguments, literal from the plan's table
while IFS='|' read -r p budget netem; do
  dry "$p"
  check "$p: exit 0" test "$CODE" -eq 0
  same "$p: uplink eth0 dport + downlink wlan0 sport" "$(tc_lines | grep -v '^tc qdisc change' || true)" \
    "$(tree eth0 dport "$netem"; tree wlan0 sport "$netem")"
  if [[ $budget == - ]]; then
    check "$p: no budget change" lacks "$OUT" "/api/budget"
  else
    check "$p: budget $budget" has_line "$OUT" "curl 'http://192.168.77.2:8080/api/budget?bps=$budget'"
  fi
  check "$p: /api/link alternative" has_line "$OUT" "curl 'http://192.168.77.2:8080/api/link?profile=external&as=$p'"
  check "$p: /api/link marked optional" has_line "$OUT" "# or, if your server has /api/link:"
  check "$p: rates in bit/s (tc 'bps' would be bytes/s)" lacks "$(tc_lines)" "bps"
done <<'EOF'
clean|0|limit 1000
degraded|0|rate 64kbit -14 delay 20ms loss 2% limit 20
hf|8000|rate 9600bit -14 delay 500ms loss 1% limit 32
lora|1500|rate 2kbit -14 delay 300ms loss 10% limit 4
telemetry|450|rate 600bit -14 delay 50ms loss 5% limit 4
contested|1500|rate 2kbit -14 delay 300ms loss 10% limit 4
blackout|-|loss 100%
EOF

# Only UDP (protocol 17) on the MinBand port reaches netem; netem only ever sits on band 1:4.
dry hf
check "filters match UDP only" test "$(grep -c 'match ip protocol 17 0xff' <<< "$OUT")" -eq 2
check "netem only on 1:4" test "$(grep 'netem' <<< "$(tc_lines)" | grep -vc 'parent 1:4 handle 40: netem')" -eq 0

# ---------------------------------------------------------------- contested: lora + blackout loop

dry contested
same "contested: loop leaf changes (blackout, then back to lora), root untouched" \
  "$(grep '^tc qdisc change' <<< "$OUT")" "$(cat <<'EOF'
tc qdisc change dev eth0 parent 1:4 handle 40: netem loss 100%
tc qdisc change dev wlan0 parent 1:4 handle 40: netem loss 100%
tc qdisc change dev eth0 parent 1:4 handle 40: netem rate 2kbit -14 delay 300ms loss 10% limit 4
tc qdisc change dev wlan0 parent 1:4 handle 40: netem rate 2kbit -14 delay 300ms loss 10% limit 4
EOF
)"
check "contested: phase lengths" has_line "$OUT" "# lora for 4-12 s, then a blackout for 1-5 s:"
check "contested: pidfile named" grep -Fq "pidfile $STATE_DIR/contested.pid" <<< "$OUT"
CONTESTED_UP=2-3 CONTESTED_DOWN=1-1 dry contested
check "contested: CONTESTED_UP/DOWN" has_line "$OUT" "# lora for 2-3 s, then a blackout for 1-1 s:"
check "dry run starts no loop and writes no state" test ! -e "$STATE_DIR"

# ---------------------------------------------------------------- clear, status

dry clear
same "clear" "$(tc_lines)" "$(printf '%s\n' 'tc qdisc del dev eth0 root 2>/dev/null || true' 'tc qdisc del dev wlan0 root 2>/dev/null || true')"
check "clear: budget back to unlimited" has_line "$OUT" "curl 'http://192.168.77.2:8080/api/budget?bps=0'"

dry status
same "status: qdisc counters and filters, both devices" "$(tc_lines)" "$(cat <<'EOF'
tc -s qdisc show dev eth0
tc filter show dev eth0
tc -s qdisc show dev wlan0
tc filter show dev wlan0
EOF
)"
check "status: no loop" has_line "$OUT" "contested loop: not running"

# ---------------------------------------------------------------- environment overrides

UP_DEV=enp1s0 DOWN_DEV=wlan1 PORT=9000 L2_OVERHEAD=0 SERVER=http://10.0.0.5:8080 dry telemetry
same "UP_DEV, DOWN_DEV, PORT, L2_OVERHEAD=0" "$(tc_lines)" "$(cat <<'EOF'
tc qdisc del dev enp1s0 root 2>/dev/null || true
tc qdisc add dev enp1s0 root handle 1: prio bands 4
tc qdisc add dev enp1s0 parent 1:4 handle 40: netem rate 600bit delay 50ms loss 5% limit 4
tc filter add dev enp1s0 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip dport 9000 0xffff flowid 1:4
tc qdisc del dev wlan1 root 2>/dev/null || true
tc qdisc add dev wlan1 root handle 1: prio bands 4
tc qdisc add dev wlan1 parent 1:4 handle 40: netem rate 600bit delay 50ms loss 5% limit 4
tc filter add dev wlan1 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip sport 9000 0xffff flowid 1:4
EOF
)"
check "SERVER" has_line "$OUT" "curl 'http://10.0.0.5:8080/api/budget?bps=450'"
OUT="$(DRY_RUN=1 "$SCRIPT" degraded)"
check "DRY_RUN=1 same as --dry-run" has_line "$OUT" "tc qdisc add dev eth0 parent 1:4 handle 40: netem rate 64kbit -14 delay 20ms loss 2% limit 20"
OUT="$("$SCRIPT" hf -n)"
check "-n after the command" has_line "$OUT" "tc qdisc add dev wlan0 parent 1:4 handle 40: netem rate 9600bit -14 delay 500ms loss 1% limit 32"

# Test hooks change only what they say.
LEAF_QDISC_OVERRIDE='pfifo limit 64' ROOT_QDISC_OVERRIDE=htb dry lora
same "test hooks: htb root, pfifo leaf, same filter" "$(tc_lines | head -n 5)" "$(cat <<'EOF'
tc qdisc del dev eth0 root 2>/dev/null || true
tc qdisc add dev eth0 root handle 1: htb
tc class add dev eth0 parent 1: classid 1:4 htb rate 10gbit quantum 65536
tc qdisc add dev eth0 parent 1:4 handle 40: pfifo limit 64
tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff match ip dport 7777 0xffff flowid 1:4
EOF
)"

# ---------------------------------------------------------------- setup (HACKATHON_PLAN 3.2)

PASSWORD='correct-horse-42' dry setup --ssid demo-link --band bg
check "setup: exit 0" test "$CODE" -eq 0
check "setup: hotspot" has_line "$OUT" "nmcli device wifi hotspot ifname wlan0 con-name minband-ap ssid demo-link band bg password '<password>'"
check "setup: password never printed" lacks "$OUT$ERR" "correct-horse-42"
check "setup: static eth0" has_line "$OUT" "nmcli connection add type ethernet ifname eth0 con-name minband-eth ipv4.method manual ipv4.addresses 192.168.77.1/24 connection.autoconnect-priority 100"
check "setup: eth0 up" has_line "$OUT" "nmcli connection up minband-eth"
check "setup: WLAN country reminder" grep -Fq "raspi-config nonint do_wifi_country KR" <<< "$OUT"
dry setup --password 'pw-from-args' --ssid x
check "setup: --password" has_line "$OUT" "nmcli device wifi hotspot ifname wlan0 con-name minband-ap ssid x band a password '<password>'"
dry setup --password short
check "setup: password under 8 characters rejected" test "$CODE" -ne 0
PASSWORD=long-enough dry setup --band n
check "setup: unknown band rejected" test "$CODE" -ne 0

# ---------------------------------------------------------------- errors, sourcing

dry bogus;                                   check "unknown command fails" test "$CODE" -ne 0
PORT=abc dry lora;                           check "bad PORT fails" test "$CODE" -ne 0
PORT=70000 dry lora;                         check "PORT > 65535 fails" test "$CODE" -ne 0
CONTESTED_DOWN=5-1 dry contested;            check "bad CONTESTED_DOWN fails" test "$CODE" -ne 0
ROOT_QDISC_OVERRIDE=cbq dry lora;            check "unknown ROOT_QDISC_OVERRIDE fails" test "$CODE" -ne 0
dry lora hf;                                 check "two commands fail" test "$CODE" -ne 0
CODE=0; "$SCRIPT" >/dev/null 2>&1 || CODE=$?; check "no command: usage, exit 2" test "$CODE" -eq 2
OUT="$("$SCRIPT" --help)";                   check "--help" grep -q 'Raspberry Pi 5 link box' <<< "$OUT"
CODE=0; "$SCRIPT" lora >/dev/null 2>"$TMP/err" || CODE=$?
if [[ $EUID -ne 0 ]]; then
  check "without --dry-run a profile needs root" grep -q 'needs root' "$TMP/err"
else
  check "without --dry-run tc runs (here: the stub, so the safety net works)" grep -q 'real tc called' "$TMP/err"
fi
OUT="$(bash -c 'source "$1"; profile hf; leaf; echo "${LEAF[*]}"' _ "$SCRIPT")"
same "sourcing runs nothing and exposes the profile table" "$OUT" "netem rate 9600bit -14 delay 500ms loss 1% limit 32"

echo "pi-link dry-run: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
