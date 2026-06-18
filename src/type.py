#!/usr/bin/env python3
"""Send literal text to the C64 Ultimate via the USB HID keyboard gadget (/dev/hidg0)."""
import sys, time

DEV = '/dev/hidg0'
HID = {chr(ord('a') + i): 0x04 + i for i in range(26)}
for i, d in enumerate('1234567890'):
    HID[d] = 0x1e + i
HID[' '] = 0x2c
HID['\n'] = 0x28  # RETURN


def tap(code, mod=0, hold=0.04, gap=0.04):
    with open(DEV, 'wb') as f:
        f.write(bytes([mod, 0, code, 0, 0, 0, 0, 0])); f.flush()
        time.sleep(hold)
        f.write(bytes(8)); f.flush()
        time.sleep(gap)


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else 'wasd'
    for ch in text:
        c = ch.lower()
        if c in HID:
            tap(HID[c])


if __name__ == '__main__':
    main()
