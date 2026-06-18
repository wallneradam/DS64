#!/bin/bash
# Create a USB HID boot-keyboard gadget on the Pi (/dev/hidg0).
# The Pi's USB-C/OTG port (dwc2 in peripheral mode) appears to the C64 Ultimate
# as a plain USB keyboard. Run as root, after the dwc2 overlay is active.
set -eu

G=/sys/kernel/config/usb_gadget/c64kbd

if [ ! -d /sys/kernel/config/usb_gadget ]; then
  modprobe libcomposite
fi

# Idempotent: if the gadget already exists and is bound, do nothing.
if [ -e "$G/UDC" ] && [ -n "$(cat "$G/UDC" 2>/dev/null)" ]; then
  echo "Gadget already bound to $(cat "$G/UDC"). Nothing to do."
  exit 0
fi

mkdir -p "$G"
echo 0x1d6b > "$G/idVendor"     # Linux Foundation
echo 0x0104 > "$G/idProduct"    # Multifunction Composite Gadget
echo 0x0100 > "$G/bcdDevice"
echo 0x0200 > "$G/bcdUSB"

mkdir -p "$G/strings/0x409"
echo "ds64"                  > "$G/strings/0x409/manufacturer"
echo "C64U Virtual Keyboard" > "$G/strings/0x409/product"
echo "0001"                  > "$G/strings/0x409/serialnumber"

mkdir -p "$G/configs/c.1/strings/0x409"
echo "Keyboard" > "$G/configs/c.1/strings/0x409/configuration"
echo 250        > "$G/configs/c.1/MaxPower"

mkdir -p "$G/functions/hid.usb0"
echo 1 > "$G/functions/hid.usb0/protocol"      # 1 = keyboard
echo 1 > "$G/functions/hid.usb0/subclass"      # 1 = boot interface
echo 8 > "$G/functions/hid.usb0/report_length" # 8-byte boot keyboard report

# Standard 63-byte HID boot keyboard report descriptor.
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
  > "$G/functions/hid.usb0/report_desc"

ln -sf "$G/functions/hid.usb0" "$G/configs/c.1/"

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
echo "Gadget bound to $UDC. /dev/hidg0 should now exist."
