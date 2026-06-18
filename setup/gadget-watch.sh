#!/bin/bash
# DS64 gadget watchdog: keep the C64 USB host enumerating our keyboard gadget.
#
# The Pi is powered by the C64, so on every power-on the C64 finishes booting
# and scans USB long before the Pi has booted Linux and created the gadget.
# When the gadget finally binds to the UDC the host has already moved on, so the
# UDC sits in "not attached" and /dev/hidg0 writes fail (ESHUTDOWN) -> no keys
# reach the C64. Re-asserting the USB pullup (unbind, pause, rebind) makes the
# C64 see a fresh hot-plug and enumerate the keyboard. Repeat until "configured".
set -u

G=/sys/kernel/config/usb_gadget/c64kbd
POLL=3        # seconds between checks while healthy / while no host present
GAP=3         # seconds the gadget stays detached during a re-assert

udc() { ls /sys/class/udc 2>/dev/null | head -1; }

reassert() {
    local u="$1"
    echo "" > "$G/UDC" 2>/dev/null      # drop the pullup
    sleep "$GAP"
    echo "$u" > "$G/UDC" 2>/dev/null    # re-assert -> host sees a hot-plug
    sleep "$GAP"
}

while true; do
    U=$(udc)
    if [ -z "$U" ] || [ ! -e "$G/UDC" ]; then
        # gadget not set up yet (ds64-gadget.service not done) -- wait.
        sleep "$POLL"
        continue
    fi
    state=$(cat "/sys/class/udc/$U/state" 2>/dev/null)
    if [ "$state" = "configured" ]; then
        sleep "$POLL"
        continue
    fi
    echo "gadget-watch: UDC $U state='$state' -> re-asserting pullup" >&2
    reassert "$U"
done
