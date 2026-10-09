#!/usr/bin/env bash
# Raspberry Pi 5 link box (docs/HACKATHON_PLAN.md section 3). The Pi sits between the phone (Wi-Fi
# hotspot on wlan0) and the laptop (Ethernet on eth0) and is "the radio": tc/netem shapes the rate,
# delay and loss of MinBand's UDP port at OS level, outside our own server. Raspberry Pi OS
# Bookworm (NetworkManager).
#
#   sudo tools/pi-link.sh setup --password '<8+ chars>' [--ssid minband-link] [--band a|bg]
#   sudo tools/pi-link.sh <profile>    # clean | degraded | hf | lora | telemetry | contested | blackout
#   tools/pi-link.sh status            # profile, contested loop, qdiscs and counters
#   sudo tools/pi-link.sh clear        # remove the shaping and stop the contested loop
#   tools/pi-link.sh --dry-run <cmd>   # print the tc/nmcli commands instead of running them
#
# Only UDP PORT goes through netem: the uplink (phone -> laptop) on UP_DEV egress matching dport,
# the downlink (acks, laptop -> phone) on DOWN_DEV egress matching sport. Each device gets a prio
# root with 4 bands; the default priomap only uses bands 1:1-1:3, so band 1:4 (netem) receives
# nothing but the u32 filter's matches, and SSH and everything else stay unshaped in 1:1-1:3.
# Applying, switching or clearing a profile deletes and re-adds the root qdisc: that swaps the
# queueing discipline, it does not take the interface down, so an SSH session on eth0 or wlan0
# survives (at most a few queued packets are dropped and TCP resends them). The contested loop
# only changes the netem leaf, never the root. `contested` = lora alternating with random 1-5 s
# blackouts, run as a background loop with a pidfile; any other profile or `clear` stops it.
#
# netem needs the sch_netem kernel module. Raspberry Pi OS ships it (check: modinfo sch_netem);
# many container and CI kernels do not, so tools/test/pi-link-kernel.test.sh tests the tree with
# pfifo in netem's place.
#
# Environment [default]:
#   UP_DEV [eth0]  DOWN_DEV [wlan0]  PORT [7777]
#   SERVER [http://192.168.77.2:8080]    the laptop's MinBand server, for the budget hint
#   DRY_RUN [0]                          1 = --dry-run (no root needed, nothing changes)
#   SSID [minband-link]  PASSWORD  BAND [a]  COUNTRY [KR]  ETH_ADDR [192.168.77.1/24]   setup;
#                                        --ssid/--password/--band override the environment
#   L2_OVERHEAD [-14]   per-packet overhead (bytes) for netem's rate. netem counts the frame with
#                       its 14 B Ethernet header; -14 makes the rate count IP bytes (payload +
#                       28 B UDP/IP), as the server and tools/eval do. 0 = count the header too.
#   CONTESTED_UP [4-12]  CONTESTED_DOWN [1-5]   contested phase lengths in seconds, min-max
#   STATE_DIR [/run/minband-pi-link]     contested pidfile and log, current profile
# Test hooks, for tools/test/pi-link-kernel.test.sh only (never set them on the Pi):
#   LEAF_QDISC_OVERRIDE   qdisc spec put on 1:4 instead of `netem <profile>`, e.g. 'pfifo limit 64'
#   ROOT_QDISC_OVERRIDE   'htb': an htb root with class 1:4 instead of prio (unclassified traffic
#                         goes direct), for kernels without sch_prio; same filter and handles
set -euo pipefail

UP_DEV="${UP_DEV:-eth0}"
DOWN_DEV="${DOWN_DEV:-wlan0}"
PORT="${PORT:-7777}"
SERVER="${SERVER:-http://192.168.77.2:8080}"
DRY_RUN="${DRY_RUN:-0}"
SSID="${SSID:-minband-link}"
PASSWORD="${PASSWORD:-}"
BAND="${BAND:-a}"
COUNTRY="${COUNTRY:-KR}"
ETH_ADDR="${ETH_ADDR:-192.168.77.1/24}"
L2_OVERHEAD="${L2_OVERHEAD--14}"
CONTESTED_UP="${CONTESTED_UP:-4-12}"
CONTESTED_DOWN="${CONTESTED_DOWN:-1-5}"
STATE_DIR="${STATE_DIR:-/run/minband-pi-link}"
LEAF_QDISC_OVERRIDE="${LEAF_QDISC_OVERRIDE:-}"
ROOT_QDISC_OVERRIDE="${ROOT_QDISC_OVERRIDE:-}"
# The contested loop is this script run again, detached; it must see the same settings.
export UP_DEV DOWN_DEV PORT L2_OVERHEAD CONTESTED_UP CONTESTED_DOWN STATE_DIR LEAF_QDISC_OVERRIDE ROOT_QDISC_OVERRIDE
PIDFILE="$STATE_DIR/contested.pid"
LOGFILE="$STATE_DIR/contested.log"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PROFILES="clean degraded hf lora telemetry contested blackout"

say() { printf '%s\n' "$*"; }
warn() { printf 'pi-link: %s\n' "$*" >&2; }
die() { warn "$*"; exit 1; }
usage() { sed -n '2,/^set -euo pipefail/p' "$SELF" | sed -e '$d' -e 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------- profiles

# The HACKATHON_PLAN 3.3 table. Sets NETEM (netem arguments; tc units: bit = bit/s, bps would be
# bytes/s), BUDGET (edge budget for /api/budget in bit/s; empty = leave it as it is) and ABOUT.
# netem's `limit` (packets) also counts datagrams still waiting out the delay, so it caps the
# datagrams in flight too. Only for hf does that bind before the rate: 8 per 500 ms = 16
# datagrams/s, so datagrams under 75 B at a higher count are tail-dropped on top of the 1 % loss.
# clean keeps the same tree with a pass-through leaf, so `status` still counts.
profile() {
  local oh=()
  [[ $L2_OVERHEAD == 0 || -z $L2_OVERHEAD ]] || oh=("$L2_OVERHEAD")
  case "$1" in
    clean)     NETEM=(limit 1000); BUDGET=0; ABOUT="Wi-Fi reference, pass-through" ;;
    degraded)  NETEM=(rate 64kbit ${oh[@]+"${oh[@]}"} delay 20ms loss 2% limit 20); BUDGET=0; ABOUT="busy mesh" ;;
    hf)        NETEM=(rate 9600bit ${oh[@]+"${oh[@]}"} delay 500ms loss 1% limit 32); BUDGET=8000; ABOUT="NATO HF ceiling" ;;
    lora)      NETEM=(rate 2kbit ${oh[@]+"${oh[@]}"} delay 300ms loss 10% limit 4); BUDGET=1500; ABOUT="Meshtastic-class LoRa" ;;
    telemetry) NETEM=(rate 600bit ${oh[@]+"${oh[@]}"} delay 50ms loss 5% limit 4); BUDGET=450; ABOUT="ELRS-class control-link telemetry" ;;
    contested) profile lora; ABOUT="intermittent jamming: lora with random $CONTESTED_DOWN s blackouts" ;;
    blackout)  NETEM=(loss 100%); BUDGET=""; ABOUT="link cut" ;;
    *) return 1 ;;
  esac
}

is_profile() { case " $PROFILES " in *" $1 "*) return 0 ;; esac; return 1; }

# LEAF: the qdisc spec for band 1:4 (netem with the profile's arguments, or the test hook's).
leaf() {
  if [[ -n $LEAF_QDISC_OVERRIDE ]]; then
    read -r -a LEAF <<< "$LEAF_QDISC_OVERRIDE"
  else
    LEAF=(netem "${NETEM[@]}")
  fi
}

# ---------------------------------------------------------------- command runner

# Shell-quoted command line; the hotspot password is masked.
quote() {
  local a out=""
  for a in "$@"; do
    if [[ -n $PASSWORD && $a == "$PASSWORD" ]]; then a="'<password>'"; else a="$(printf '%q' "$a")"; fi
    out+="${out:+ }$a"
  done
  say "$out"
}
run() { if [[ $DRY_RUN == 1 ]]; then quote "$@"; else "$@"; fi; }
# For deleting what may not exist: the error is expected and ignored.
try() { if [[ $DRY_RUN == 1 ]]; then say "$(quote "$@") 2>/dev/null || true"; else "$@" 2>/dev/null || true; fi; }

need_root() {
  if [[ $DRY_RUN == 1 ]]; then return 0; fi
  [[ $EUID -eq 0 ]] || die "needs root: sudo $SELF $*"
  command -v tc >/dev/null 2>&1 || die "tc not found: sudo apt install iproute2"
}

# ---------------------------------------------------------------- shaping

# shape <dev> <dport|sport>: root with 4 bands, LEAF on 1:4, UDP PORT -> 1:4 (HACKATHON_PLAN 3.3).
shape() {
  local dev=$1 dir=$2
  try tc qdisc del dev "$dev" root
  if [[ $ROOT_QDISC_OVERRIDE == htb ]]; then   # test hook, see the header
    run tc qdisc add dev "$dev" root handle 1: htb
    run tc class add dev "$dev" parent 1: classid 1:4 htb rate 10gbit quantum 65536
  else
    run tc qdisc add dev "$dev" root handle 1: prio bands 4
  fi
  run tc qdisc add dev "$dev" parent 1:4 handle 40: "${LEAF[@]}" \
    || die "could not add ${LEAF[0]} on $dev (kernel without sch_${LEAF[0]}? on the Pi: sudo modprobe sch_${LEAF[0]})"
  run tc filter add dev "$dev" parent 1: protocol ip prio 1 u32 \
    match ip protocol 17 0xff match ip "$dir" "$PORT" 0xffff flowid 1:4
}

# Changes only the 1:4 leaf on both devices (the contested loop); root and filters stay.
set_leaves() {
  profile "$1"; leaf
  run tc qdisc change dev "$UP_DEV" parent 1:4 handle 40: "${LEAF[@]}"
  run tc qdisc change dev "$DOWN_DEV" parent 1:4 handle 40: "${LEAF[@]}"
}

# Budget hint for the laptop: <profile name for /api/link>; uses BUDGET.
budget_hint() {
  if [[ -n $BUDGET ]]; then
    say "# Set the edge budget ($([[ $BUDGET == 0 ]] && echo 'unlimited' || echo "$BUDGET bit/s")), on the laptop:"
    say "curl '$SERVER/api/budget?bps=$BUDGET'"
  else
    say "# Leave the edge budget as it is: the edge cannot see that the link is cut."
  fi
  say "# or, if your server has /api/link:"
  say "curl '$SERVER/api/link?profile=external&as=$1'"
}

apply() {
  local p=$1
  need_root "$p"
  stop_contested
  profile "$p"; leaf
  shape "$UP_DEV" dport
  shape "$DOWN_DEV" sport
  if [[ $p == contested ]]; then start_contested; fi
  record "$p"
  profile "$p"
  say "# $p ($ABOUT): UDP $PORT up on $UP_DEV egress (dport), down on $DOWN_DEV egress (sport): ${LEAF[*]}"
  budget_hint "$p"
}

clear_all() {
  need_root clear
  stop_contested
  try tc qdisc del dev "$UP_DEV" root
  try tc qdisc del dev "$DOWN_DEV" root
  if [[ $DRY_RUN != 1 ]]; then rm -f "$STATE_DIR/profile"; fi
  say "# cleared: $UP_DEV and $DOWN_DEV are back on their default qdiscs"
  BUDGET=0
  budget_hint clean
}

record() {
  if [[ $DRY_RUN == 1 ]]; then return 0; fi
  mkdir -p "$STATE_DIR"
  say "$1 since $(date '+%F %T')" > "$STATE_DIR/profile"
}

# ---------------------------------------------------------------- contested loop

# Random integer in a "min-max" range.
pick() { local lo=${1%-*} hi=${1#*-}; say $((lo + RANDOM % (hi - lo + 1))); }

check_range() {
  if [[ $2 =~ ^[0-9]+-[0-9]+$ ]] && (( 10#${2%-*} <= 10#${2#*-} )); then return 0; fi
  die "$1 must be min-max seconds, e.g. 1-5 (got '$2')"
}

is_loop() {
  local c
  c="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)" || return 1
  [[ $c == *_contested-loop* ]]
}

loop_pid() {
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null)" || return 1
  [[ $pid =~ ^[0-9]+$ ]] && is_loop "$pid" && say "$pid"
}

start_contested() {
  if [[ $DRY_RUN == 1 ]]; then
    say "# contested: background loop (pidfile $PIDFILE, log $LOGFILE), forever:"
    say "# lora for $CONTESTED_UP s, then a blackout for $CONTESTED_DOWN s:"
    set_leaves blackout
    say "# then back to lora:"
    set_leaves lora
    return 0
  fi
  mkdir -p "$STATE_DIR"
  rm -f "$PIDFILE"
  : > "$LOGFILE"
  # setsid: own session and process group, no controlling terminal, so the loop outlives this
  # shell and sudo's pty, and `kill -- -<pid>` stops it together with its sleep.
  setsid "$SELF" _contested-loop < /dev/null >> "$LOGFILE" 2>&1 &
  local _
  for _ in $(seq 30); do
    if loop_pid >/dev/null; then return 0; fi
    sleep 0.1
  done
  die "contested loop did not start, see $LOGFILE"
}

stop_contested() {
  local pid _
  pid="$(loop_pid)" || { if [[ $DRY_RUN != 1 ]]; then rm -f "$PIDFILE"; fi; return 0; }
  run kill -TERM -- "-$pid"
  if [[ $DRY_RUN == 1 ]]; then return 0; fi
  for _ in $(seq 50); do
    if ! is_loop "$pid"; then rm -f "$PIDFILE"; return 0; fi
    sleep 0.1
  done
  die "contested loop (pid $pid) did not stop"
}

contested_loop() {
  say "$$" > "$PIDFILE"
  local t
  while :; do
    t="$(pick "$CONTESTED_UP")"; say "$(date '+%F %T') lora for $t s"; sleep "$t"
    t="$(pick "$CONTESTED_DOWN")"; set_leaves blackout; say "$(date '+%F %T') blackout for $t s"; sleep "$t"
    set_leaves lora
  done
}

# ---------------------------------------------------------------- status

status() {
  local pid p="none"
  if [[ -r $STATE_DIR/profile ]]; then p="$(cat "$STATE_DIR/profile")"; fi
  say "profile: $p"
  if pid="$(loop_pid)"; then
    say "contested loop: running (pid $pid), last switches:"
    tail -n 4 "$LOGFILE" 2>/dev/null | sed 's/^/  /'
  else
    say "contested loop: not running"
  fi
  say "== uplink: $UP_DEV egress, UDP dport $PORT -> 1:4 =="
  run tc -s qdisc show dev "$UP_DEV"
  run tc filter show dev "$UP_DEV"
  say "== downlink: $DOWN_DEV egress, UDP sport $PORT -> 1:4 =="
  run tc -s qdisc show dev "$DOWN_DEV"
  run tc filter show dev "$DOWN_DEV"
}

# ---------------------------------------------------------------- setup (HACKATHON_PLAN 3.2)

# No `nmcli | grep -q`: with pipefail an early grep exit can fail nmcli with SIGPIPE.
has_con() {
  local names
  command -v nmcli >/dev/null 2>&1 || return 1
  names="$(nmcli -g NAME connection show 2>/dev/null)" || return 1
  grep -qx -- "$1" <<< "$names"
}

setup() {
  (( ${#PASSWORD} >= 8 && ${#PASSWORD} <= 63 )) || die "hotspot password: 8-63 characters (--password or PASSWORD=)"
  [[ $BAND == a || $BAND == bg ]] || die "band must be a (5 GHz) or bg (2.4 GHz), got '$BAND'"
  need_root setup
  if [[ $DRY_RUN != 1 ]]; then command -v nmcli >/dev/null 2>&1 || die "nmcli not found (Raspberry Pi OS Bookworm uses NetworkManager)"; fi
  local laptop=${SERVER#*://} cur=""
  laptop=${laptop%%[:/]*}
  if command -v iw >/dev/null 2>&1; then
    cur="$(iw reg get 2>/dev/null | sed -n '/^country /{s/^country \([A-Z0-9]*\):.*/\1/p;q;}')" || true
  fi
  say "# The WLAN country must be set ($COUNTRY here) or 5 GHz AP mode will not start:"
  say "#   sudo raspi-config nonint do_wifi_country $COUNTRY   (or raspi-config -> Localisation -> WLAN Country)"
  if [[ -n $cur && $cur != "$COUNTRY" ]]; then warn "WLAN country is $cur, not $COUNTRY: set it first (above), then rerun setup"; fi
  say "# Run setup from the Pi's console or over Ethernet: $DOWN_DEV becomes the hotspot (ending any Wi-Fi"
  say "# client connection) and $UP_DEV becomes $ETH_ADDR."
  say "# Hotspot on $DOWN_DEV: NetworkManager shared mode gives DHCP, forwarding and NAT; the Pi is 10.42.0.1."
  try nmcli connection delete minband-ap
  run nmcli device wifi hotspot ifname "$DOWN_DEV" con-name minband-ap ssid "$SSID" band "$BAND" password "$PASSWORD"
  run nmcli connection modify minband-ap connection.autoconnect yes
  say "# Static Ethernet to the laptop; priority 100 wins over a DHCP 'Wired connection 1' after a reboot."
  if has_con minband-eth; then
    run nmcli connection modify minband-eth ipv4.method manual ipv4.addresses "$ETH_ADDR" connection.autoconnect-priority 100
  else
    run nmcli connection add type ethernet ifname "$UP_DEV" con-name minband-eth \
      ipv4.method manual ipv4.addresses "$ETH_ADDR" connection.autoconnect-priority 100
  fi
  run nmcli connection up minband-eth
  say "# Next: laptop Ethernet $laptop/${ETH_ADDR#*/}, no gateway (macOS: Network -> Ethernet adapter -> Details ->"
  say "# TCP/IP -> Manually). Phone: join '$SSID', point the app at $laptop:${PORT}. Check on the laptop:"
  say "# curl localhost:8080/api/metrics (devices[].addr from ${ETH_ADDR%/*}). Nothing arrives: on macOS"
  say "# sudo route -n add 10.42.0.0/24 ${ETH_ADDR%/*} and allow node in the firewall. Then: sudo $SELF lora"
}

# ---------------------------------------------------------------- main

main() {
  local cmd=""
  while (($#)); do
    case "$1" in
      -n|--dry-run) DRY_RUN=1 ;;
      -h|--help) usage; exit 0 ;;
      --ssid) SSID="${2:?--ssid needs a value}"; shift ;;
      --password) PASSWORD="${2:?--password needs a value}"; shift ;;
      --band) BAND="${2:?--band needs a value}"; shift ;;
      -*) die "unknown option $1 (see --help)" ;;
      *) [[ -z $cmd ]] || die "one command at a time (got '$cmd' and '$1')"; cmd=$1 ;;
    esac
    shift
  done
  if ! [[ $PORT =~ ^[0-9]+$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then die "PORT must be 1-65535, got '$PORT'"; fi
  [[ -z $L2_OVERHEAD || $L2_OVERHEAD =~ ^-?[0-9]+$ ]] || die "L2_OVERHEAD must be an integer, got '$L2_OVERHEAD'"
  [[ -z $ROOT_QDISC_OVERRIDE || $ROOT_QDISC_OVERRIDE == htb ]] || die "ROOT_QDISC_OVERRIDE: only 'htb' (a test hook)"
  check_range CONTESTED_UP "$CONTESTED_UP"
  check_range CONTESTED_DOWN "$CONTESTED_DOWN"
  case "$cmd" in
    setup) setup ;;
    status) status ;;
    clear) clear_all ;;
    _contested-loop) contested_loop ;;
    "") usage >&2; exit 2 ;;
    *) is_profile "$cmd" || die "unknown command '$cmd': setup | $PROFILES | status | clear"; apply "$cmd" ;;
  esac
}

# Sourcing (the kernel test) defines the functions without running anything.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
