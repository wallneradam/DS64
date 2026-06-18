#!/bin/bash
# Pair a single PlayStation controller and make the bond DURABLE -- without leaving
# the controller disconnected afterwards.
#
# The catch: when bluetoothctl pairs a DS4 from a *non-interactive* command (e.g.
# `bluetoothctl pair MAC`), the kernel reports the link key with store_hint=0 and
# BlueZ deliberately does NOT persist it; the bond is then lost on power-off and the
# controller can't reconnect. Pairing from an *interactive* bluetoothctl session
# gets store_hint=1 and BlueZ stores the key itself. See
# https://github.com/bluez/bluez/issues/748 -- this is BlueZ behaviour, not the
# controller's fault.
#
# So we pair from a single, kept-alive bluetoothctl session (interactive). If that
# still doesn't persist the key on this box, we fall back to capturing it with btmon
# and writing it into the BlueZ 'info' file ourselves (which needs a bluetoothd
# restart, hence a one-off extra PS press).
#
# Single-controller policy: any previously known controller is removed first.
# Run as root.
set -u

CTRL_RE="Wireless Controller|DualShock|DualSense|DUALSHOCK|Sony"
SCAN_SECS="${1:-20}"
ADAPTER_DIR=$(ls -d /var/lib/bluetooth/*/ 2>/dev/null | head -1)

known_controllers() {
  bluetoothctl devices 2>/dev/null | grep -iE "$CTRL_RE" | awk '{print $2}'
}
info_file()   { echo "${ADAPTER_DIR}${1}/info"; }
has_linkkey() { grep -q "\[LinkKey\]" "$(info_file "$1")" 2>/dev/null; }
has_trust()   { grep -qi "^Trusted=true" "$(info_file "$1")" 2>/dev/null; }

echo "Powering on; removing any previously known controller (single-controller policy)..."
bluetoothctl power on >/dev/null
for mac in $(known_controllers); do
  echo "  removing old: $mac"
  bluetoothctl remove "$mac" >/dev/null 2>&1
done

# Capture HCI/mgmt traffic so the fallback can grab the link key if needed.
BTMON_LOG=$(mktemp /tmp/ds64-btmon.XXXXXX)
btmon >"$BTMON_LOG" 2>&1 &
BTMON_PID=$!
sleep 0.4

echo "Scanning ${SCAN_SECS}s -- put the controller in pairing mode now (SHARE+PS held until the lightbar double-flashes)..."
bluetoothctl --timeout "$SCAN_SECS" scan on >/dev/null 2>&1

MAC=$(known_controllers | head -1)
if [ -z "$MAC" ]; then
  kill "$BTMON_PID" 2>/dev/null; wait "$BTMON_PID" 2>/dev/null
  echo "NO controller found. Devices seen:"; bluetoothctl devices
  exit 1
fi
echo "Found controller: $MAC"

# Pair from ONE kept-alive (interactive) bluetoothctl session. The sleeps hold the
# session open until each step finishes, which is what makes it count as interactive.
#
# CRITICAL first wait: bluetoothctl connects to bluetoothd asynchronously after it
# starts. When commands are piped in, it reads them immediately and runs the first
# one BEFORE that connection is up -- 'agent on' then fails ("Failed to register
# agent object"), no agent answers the controller's pairing, the link key arrives
# with store_hint=0 (BlueZ won't persist it) and the bond is not durable. The
# leading sleep lets the connection settle first; with it the agent registers,
# pairing reports store_hint=1 and BlueZ writes the key to disk itself.
#
# CRITICAL ordering: trust BEFORE pair. After pairing, the controller immediately
# opens its HID input service; BlueZ asks input/server.c:auth_callback() to
# authorize it. If the device isn't trusted at that instant (and no agent answers),
# auth_callback denies it ("Access denied") and the controller terminates the link
# (HCI reason 0x13, Remote User Terminated) -- so pairing "succeeds" but the
# controller drops and can't get back. Trusting first makes that authorization
# auto-succeed. We trust again after pair so Trusted=true is written to the info
# file (it persists the auto-authorize for every future reconnect, agent-free).
{
  sleep 1.5                         # let bluetoothctl reach bluetoothd before any command
  printf 'agent on\n';          sleep 0.5
  printf 'default-agent\n';     sleep 0.5
  printf 'trust %s\n'   "$MAC"; sleep 0.6
  printf 'pair %s\n'    "$MAC"; sleep 8
  printf 'trust %s\n'   "$MAC"; sleep 0.6
  printf 'connect %s\n' "$MAC"; sleep 4
  printf 'quit\n'
} | bluetoothctl 2>&1 \
  | grep -iE "Pairing successful|Failed|trust succeeded|Connection successful" \
  | grep -viE "in progress" | sed 's/^/  /'

sleep 1
kill "$BTMON_PID" 2>/dev/null; wait "$BTMON_PID" 2>/dev/null

if has_linkkey "$MAC"; then
  echo "OK: BlueZ persisted the link key itself (interactive pairing) -- controller stays connected, bond survives reboot."
  rm -f "$BTMON_LOG"
else
  echo "BlueZ did not persist the key (store_hint=0); falling back to capturing it from btmon..."
  echo "=== btmon link-key lines (diagnostics) ==="
  grep -iE "Link key[][0-9:]*|Key type:|Store hint:" "$BTMON_LOG" | sed 's/^/  /'
  KEY=$(grep -iE "Link key(\[[0-9]+\])?:" "$BTMON_LOG" | grep -ioE "[0-9a-f]{32}" | tail -1 | tr 'a-f' 'A-F')
  KTYPE_HEX=$(grep -i "Key type:" "$BTMON_LOG" | grep -oiE "0x[0-9a-f]{2}" | tail -1)
  [ -n "$KTYPE_HEX" ] && KTYPE=$((KTYPE_HEX)) || KTYPE=4
  rm -f "$BTMON_LOG"
  if [ -z "$KEY" ]; then
    echo "! Could not capture a link key. Pairing works for this session but will NOT survive a reboot." >&2
    exit 3
  fi
  echo "Captured link key (type $KTYPE); persisting it (bluetoothd stopped so it cannot overwrite)..."
  systemctl stop bluetooth
  sleep 1
  {
    echo ""
    echo "[LinkKey]"
    echo "Key=$KEY"
    echo "Type=$KTYPE"
    echo "PINLength=0"
  } >> "$(info_file "$MAC")"
  sync
  systemctl start bluetooth
  sleep 3
  bluetoothctl connect "$MAC" >/dev/null 2>&1
  sleep 2
fi

if has_linkkey "$MAC"; then
  echo "OK: link key is on disk -- the bond now survives power-off."
else
  echo "! Link key still not on disk; something went wrong." >&2
  exit 4
fi

# Ensure the device is trusted on disk. Without Trusted=true, BlueZ denies the HID
# input service on every (re)connect -- no agent runs in normal operation -- so the
# controller drops (see the auth_callback note above). This is what keeps it both
# connected after pairing and able to reconnect later on a plain PS press.
if ! has_trust "$MAC"; then
  bluetoothctl trust "$MAC" >/dev/null 2>&1
  sync
  sleep 0.5
fi
if has_trust "$MAC"; then
  echo "OK: device is trusted -- BlueZ auto-authorizes it on reconnect (no agent needed)."
else
  echo "! Device is not trusted on disk; reconnect will be rejected." >&2
  exit 5
fi

if bluetoothctl info "$MAC" 2>/dev/null | grep -qi "Connected: yes"; then
  echo "Controller connected and ready."
else
  echo "Press PS on the controller once to reconnect (it will then stay paired)."
fi
