#!/usr/bin/env bash
# Real-kernel test for tools/pi-link.sh on the loopback device. Builds the script's own tc tree
# (its `shape` function) on `lo` and proves with `tc -s` counters that the u32 filter puts UDP to
# the MinBand port (uplink filter) or from it (downlink filter) into band 1:4, while UDP on
# another port and TCP on the same port stay out. Then runs the CLI end to end on lo (contested
# loop started and stopped through its pidfile, status, clear) and checks that an open TCP
# connection on the shaped device survives profile switches, as SSH on the Pi must.
#
#   sudo tools/test/pi-link-kernel.test.sh
#
# Needs root, tc (iproute2) and python3 (to send from a fixed source port); skips otherwise.
# Refuses to run if lo already has a root qdisc, and leaves lo as it found it.
# netem is replaced by pfifo through the script's LEAF_QDISC_OVERRIDE test hook (container
# kernels often lack sch_netem); where the kernel also lacks sch_prio, the root becomes an htb with
# the same handles (ROOT_QDISC_OVERRIDE=htb). Where sch_netem exists (e.g. on the Pi), one more
# check applies the real lora netem to lo and reads it back.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../pi-link.sh"

skip() { echo "SKIP pi-link kernel test: $*"; exit 0; }
[[ $EUID -eq 0 ]] || skip "needs root"
command -v tc >/dev/null 2>&1 || skip "needs tc (iproute2)"
command -v python3 >/dev/null 2>&1 || skip "needs python3"
if [[ "$(tc qdisc show dev lo)" != *"qdisc noqueue 0: root"* ]]; then
  echo "lo already has a root qdisc; not touching it:" >&2
  tc qdisc show dev lo >&2
  exit 1
fi

TMP="$(mktemp -d)"
export STATE_DIR="$TMP/state" UP_DEV=lo DOWN_DEV=lo LEAF_QDISC_OVERRIDE='pfifo limit 64'
unset PORT ROOT_QDISC_OVERRIDE L2_OVERHEAD CONTESTED_UP CONTESTED_DOWN DRY_RUN
cleanup() {
  "$SCRIPT" clear >/dev/null 2>&1 || true
  tc qdisc del dev lo root 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

if tc qdisc add dev lo root handle 1: prio bands 4 2>/dev/null; then
  tc qdisc del dev lo root
  ROOT=prio
else
  export ROOT_QDISC_OVERRIDE=htb
  ROOT="htb (kernel without sch_prio; ROOT_QDISC_OVERRIDE=htb)"
fi
HAVE_NETEM=0
if tc qdisc add dev lo root handle 1: netem limit 10 2>/dev/null; then tc qdisc del dev lo root; HAVE_NETEM=1; fi
echo "kernel $(uname -r): root $ROOT, leaf pfifo (LEAF_QDISC_OVERRIDE), sch_netem $([[ $HAVE_NETEM == 1 ]] && echo present || echo absent)"

PASS=0 FAIL=0
expect() { local name=$1; shift; if "$@"; then PASS=$((PASS + 1)); echo "ok   $name"; else FAIL=$((FAIL + 1)); echo "FAIL $name" >&2; fi; }

# udp <count> <dport> [sport]: datagrams with a 50 B payload, 127.0.0.1 -> 127.0.0.1.
udp() {
  python3 - "$@" <<'PY'
import socket, sys
n, dport = int(sys.argv[1]), int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[3]) if len(sys.argv) > 3 else 0))
for _ in range(n):
    s.sendto(b"m" * 50, ("127.0.0.1", dport))
PY
}
# tcp <count> <port>: connection attempts (SYN out, RST back when nothing listens).
tcp() {
  python3 - "$@" <<'PY'
import socket, sys
for _ in range(int(sys.argv[1])):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", int(sys.argv[2])))
    except OSError:
        pass
    s.close()
PY
}
# qd <pkts|bytes> <handle>: what that qdisc on lo has sent.
qd() {
  tc -s qdisc show dev lo | awk -v f="$1" -v h="$2" \
    '$1 == "qdisc" { on = ($3 == h) } on && $1 == "Sent" { print (f == "pkts" ? $4 : $2); exit }'
}
# A datagram with a 50 B payload is 50 + 8 (UDP) + 20 (IPv4) + 14 (Ethernet header, which the
# qdisc sees on lo as on eth0 and wlan0: the reason for L2_OVERHEAD=-14) = 92 B at the qdisc.
DGRAM=92

# ---------------------------------------------------------------- uplink filter (dport)

# shellcheck disable=SC1090  # the script under test; sourcing defines its functions only
( source "$SCRIPT"; profile lora; leaf; shape lo dport )
l0=$(qd pkts 40:) b0=$(qd bytes 40:) r0=$(qd pkts 1:)
udp 7 7777
expect "uplink: 7 UDP datagrams to :7777 land in 1:4" test "$(( $(qd pkts 40:) - l0 ))" -eq 7
expect "uplink: 7 x $DGRAM B counted in 1:4" test "$(( $(qd bytes 40:) - b0 ))" -eq $((7 * DGRAM))
l1=$(qd pkts 40:)
udp 5 7778
tcp 3 7777
udp 4 9 7777
expect "uplink: UDP to :7778, TCP to :7777 and UDP from :7777 stay out of 1:4" test "$(qd pkts 40:)" -eq "$l1"
expect "uplink: the root still carried them (>= 5 + 3 + 4 more packets)" test "$(( $(qd pkts 1:) - r0 ))" -ge $((7 + 5 + 3 + 4))
echo "--- tc -s qdisc show dev lo (uplink filter):"; tc -s qdisc show dev lo; tc filter show dev lo

# ---------------------------------------------------------------- downlink filter (sport)

# shellcheck disable=SC1090  # the script under test; sourcing defines its functions only
( source "$SCRIPT"; profile lora; leaf; shape lo sport )
l0=$(qd pkts 40:) r0=$(qd pkts 1:)
udp 6 9 7777
expect "downlink: 6 UDP datagrams from :7777 land in 1:4" test "$(( $(qd pkts 40:) - l0 ))" -eq 6
l1=$(qd pkts 40:)
udp 5 7777
tcp 3 7777
udp 4 9 7778
expect "downlink: UDP to :7777, TCP on :7777 and UDP from :7778 stay out of 1:4" test "$(qd pkts 40:)" -eq "$l1"
expect "downlink: the root still carried them" test "$(( $(qd pkts 1:) - r0 ))" -ge $((6 + 5 + 3 + 4))
echo "--- tc -s qdisc show dev lo (downlink filter):"; tc -s qdisc show dev lo

# ---------------------------------------------------------------- CLI: contested loop, status, clear

loop_alive() { [[ -r /proc/$1/cmdline ]] && [[ "$(tr '\0' ' ' < "/proc/$1/cmdline")" == *_contested-loop* ]]; }
loop_gone() { ! loop_alive "$1"; }
CONTESTED_UP=1-1 CONTESTED_DOWN=1-1 "$SCRIPT" contested > "$TMP/out"
pid="$(cat "$STATE_DIR/contested.pid" 2>/dev/null || true)"
expect "contested: pidfile written" test -n "$pid"
expect "contested: loop running after the command returned (pid $pid)" loop_alive "$pid"
expect "contested: leaf and filter on lo" grep -q 'flowid 1:4' <<< "$(tc filter show dev lo)"
expect "contested: budget hint" grep -Fxq "curl 'http://192.168.77.2:8080/api/budget?bps=1500'" "$TMP/out"
sleep 3.5
expect "contested: loop switched lora -> blackout -> lora with tc qdisc change" \
  test "$(grep -c 'blackout for 1 s' "$STATE_DIR/contested.log")" -ge 1
expect "contested: the loop is still alive (its tc changes succeeded)" loop_alive "$pid"
"$SCRIPT" status > "$TMP/status"
expect "status: reports the loop" grep -Fq "contested loop: running (pid $pid)" "$TMP/status"
expect "status: reports the profile" grep -q '^profile: contested since ' "$TMP/status"
expect "status: shows the 1:4 leaf counters" grep -q 'qdisc pfifo 40: parent 1:4' "$TMP/status"
"$SCRIPT" hf > /dev/null
expect "another profile stops the loop" loop_gone "$pid"
expect "... and removes the pidfile" test ! -e "$STATE_DIR/contested.pid"
expect "... and records the new profile" grep -q '^hf since ' "$STATE_DIR/profile"
"$SCRIPT" contested > /dev/null
pid="$(cat "$STATE_DIR/contested.pid")"
"$SCRIPT" clear > /dev/null
expect "clear stops the loop" loop_gone "$pid"
expect "clear restores lo's default qdisc" grep -q 'qdisc noqueue 0: root' <<< "$(tc qdisc show dev lo)"
expect "clear removes the state" test ! -e "$STATE_DIR/profile" -a ! -e "$STATE_DIR/contested.pid"

# ---------------------------------------------------------------- a TCP session survives

if python3 - "$SCRIPT" hf blackout contested clear lora clear <<'PY'
import socket, subprocess, sys
srv = socket.socket()
srv.bind(("127.0.0.1", 0)); srv.listen(1)
cli = socket.create_connection(srv.getsockname()); conn, _ = srv.accept()
def roundtrip(msg):
    cli.sendall(msg)
    assert conn.recv(100) == msg, msg
roundtrip(b"before")
for p in sys.argv[2:]:
    subprocess.run([sys.argv[1], p], check=True, stdout=subprocess.DEVNULL)
    roundtrip(p.encode())
PY
then r=0; else r=1; fi
expect "an open TCP connection on lo survives hf, blackout, contested, clear, lora, clear" test "$r" -eq 0

# ---------------------------------------------------------------- real netem, where the kernel has it

if [[ $HAVE_NETEM == 1 ]]; then
  LEAF_QDISC_OVERRIDE='' "$SCRIPT" lora > /dev/null
  q="$(tc qdisc show dev lo)"
  echo "--- real netem:"; echo "$q"
  netem_lora() { [[ $1 == *"netem 40: parent 1:4 limit 4 "* && $1 == *"delay 300ms"* && $1 == *"loss 10%"* && $1 == *"rate 2Kbit packetoverhead -14"* ]]; }
  expect "netem lora: rate 2Kbit, delay 300ms, loss 10%, limit 4, packet overhead -14" netem_lora "$q"
  "$SCRIPT" clear > /dev/null
else
  echo "skip real netem: this kernel has no sch_netem (Raspberry Pi OS ships it)"
fi

echo "pi-link kernel: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
