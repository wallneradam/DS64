#!/bin/bash
# Create a composite USB HID gadget on the Pi with TWO independent interfaces:
#   hid.usb0 -> boot keyboard (/dev/hidg0): WASD->joystick via the U64 swapper
#   hid.usb1 -> mouse        (/dev/hidg1): emulated as a Commodore 1351 on port 1
# The U64 host installs a driver and an interrupt pipe PER HID interface and polls
# each independently, so the keyboard and the mouse work at the same time (this is
# the same shape as a real keyboard+touchpad dongle, which the C64 handles fine).
# Run as root, after the dwc2 overlay is active.
set -eu

G=/sys/kernel/config/usb_gadget/c64kbd

if [ ! -d /sys/kernel/config/usb_gadget ]; then
  modprobe libcomposite
fi

# Idempotent: if the gadget is already bound AND the mouse interface (hid.usb1,
# 3-byte report) is present, do nothing. An older keyboard-only or combined
# Report-ID gadget lacks hid.usb1 / has a different report_length -> rebuild.
if [ -n "$(cat "$G/UDC" 2>/dev/null || true)" ] && \
   [ "$(cat "$G/functions/hid.usb1/report_length" 2>/dev/null || true)" = "3" ]; then
  echo "Gadget already bound to $(cat "$G/UDC") with keyboard+mouse. Nothing to do."
  exit 0
fi

# Tear down any partial/old gadget so we can rebuild cleanly. configfs requires
# unbinding the UDC before functions can be unlinked and removed.
teardown() {
  [ -d "$G" ] || return 0
  echo "" > "$G/UDC" 2>/dev/null || true
  rm -f "$G"/configs/c.1/hid.usb0 "$G"/configs/c.1/hid.usb1 2>/dev/null || true
  rmdir "$G"/configs/c.1/strings/0x409 2>/dev/null || true
  rmdir "$G"/configs/c.1 2>/dev/null || true
  rmdir "$G"/functions/hid.usb0 "$G"/functions/hid.usb1 2>/dev/null || true
  rmdir "$G"/strings/0x409 2>/dev/null || true
  rmdir "$G" 2>/dev/null || true
}
teardown

mkdir -p "$G"
echo 0x1d6b > "$G/idVendor"     # Linux Foundation
echo 0x0104 > "$G/idProduct"    # Multifunction Composite Gadget
echo 0x0100 > "$G/bcdDevice"
echo 0x0200 > "$G/bcdUSB"

mkdir -p "$G/strings/0x409"
echo "ds64"                        > "$G/strings/0x409/manufacturer"
echo "C64U Virtual Keyboard+Mouse" > "$G/strings/0x409/product"
echo "0001"                        > "$G/strings/0x409/serialnumber"

mkdir -p "$G/configs/c.1/strings/0x409"
echo "Keyboard+Mouse" > "$G/configs/c.1/strings/0x409/configuration"
echo 250              > "$G/configs/c.1/MaxPower"

# Interface 0: boot keyboard (subclass=1 boot, protocol=1 keyboard, 8-byte report,
# no report ID). Standard 63-byte HID boot keyboard report descriptor.
mkdir -p "$G/functions/hid.usb0"
echo 1 > "$G/functions/hid.usb0/protocol"
echo 1 > "$G/functions/hid.usb0/subclass"
echo 8 > "$G/functions/hid.usb0/report_length"
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
  > "$G/functions/hid.usb0/report_desc"

# Interface 1: mouse (subclass=1 boot, protocol=2 mouse, 3-byte report, no report
# ID). Standard relative mouse: 3 buttons + 5-bit pad + signed int8 X + Y. The
# relative X/Y satisfy the U64's locateMouseFields (it rejects absolute axes).
mkdir -p "$G/functions/hid.usb1"
echo 2 > "$G/functions/hid.usb1/protocol"
echo 1 > "$G/functions/hid.usb1/subclass"
echo 3 > "$G/functions/hid.usb1/report_length"
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x15\x81\x25\x7f\x75\x08\x95\x02\x81\x06\xc0\xc0' \
  > "$G/functions/hid.usb1/report_desc"

ln -sf "$G/functions/hid.usb0" "$G/configs/c.1/"
ln -sf "$G/functions/hid.usb1" "$G/configs/c.1/"

# Wait for the USB Device Controller to appear (dwc2 peripheral mode), then bind.
UDC=""
for _ in $(seq 1 50); do
  UDC=$(ls /sys/class/udc 2>/dev/null | head -1)
  [ -n "$UDC" ] && break
  sleep 0.2
done
if [ -z "$UDC" ]; then
  echo "No UDC found. Is 'dtoverlay=dwc2,dr_mode=peripheral' set in config.txt?" >&2
  exit 1
fi
echo "$UDC" > "$G/UDC"
echo "Gadget bound to $UDC. /dev/hidg0 (keyboard) and /dev/hidg1 (mouse) should now exist."
