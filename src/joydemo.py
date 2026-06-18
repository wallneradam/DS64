#!/usr/bin/env python3
"""Hold each direction (W/A/S/D) and fire (RETURN) for ~1.5s, to demo joystick
movement when the U64 is in 'WASD Port 2' mode. Watch with a joystick poller."""
import time

DEV = '/dev/hidg0'
SEQ = [('W  -> UP', 0x1a), ('A  -> LEFT', 0x04), ('S  -> DOWN', 0x16),
       ('D  -> RIGHT', 0x07), ('RETURN -> FIRE', 0x28)]


def send(code):
    with open(DEV, 'wb') as f:
        f.write(bytes([0, 0, code, 0, 0, 0, 0, 0])); f.flush()


def release():
    with open(DEV, 'wb') as f:
        f.write(bytes(8)); f.flush()


for name, code in SEQ:
    print(name, flush=True)
    send(code)
    time.sleep(1.5)
    release()
    time.sleep(0.7)
print('done')
