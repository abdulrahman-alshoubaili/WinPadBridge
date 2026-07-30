"""AllyPad receiver -- run this ON THE LAPTOP.

Creates a virtual Xbox 360 controller (via the ViGEmBus driver) and
mirrors the gamepad state streamed from the Ally. PCSX2 and any other
game will see it as a normal Xbox 360 pad.

Prerequisites (laptop only):
    1. Install the ViGEmBus driver (see README).
    2. pip install vgamepad

Usage:
    python receiver_laptop.py [port]
"""

import socket
import struct
import sys

try:
    import vgamepad as vg
except Exception as e:
    sys.exit(
        "Could not load vgamepad/ViGEmBus.\n"
        "1) Install the ViGEmBus driver (see README)\n"
        "2) Run: pip install vgamepad\n"
        f"Details: {e}"
    )

DEFAULT_PORT = 47845
PACKET_FMT = "<IHBBhhhh"           # seq, buttons, LT, RT, LX, LY, RX, RY
PACKET_SIZE = struct.calcsize(PACKET_FMT)
LINK_TIMEOUT_S = 0.5               # neutralize pad if no packets for this long


def set_neutral(gp):
    """Release everything so buttons never get 'stuck' if the link drops."""
    r = gp.report
    r.wButtons = 0
    r.bLeftTrigger = 0
    r.bRightTrigger = 0
    r.sThumbLX = 0
    r.sThumbLY = 0
    r.sThumbRX = 0
    r.sThumbRY = 0
    gp.update()


def is_newer(seq, last):
    """True if seq is newer than last (handles 32-bit wraparound)."""
    if last is None:
        return True
    return seq != last and ((seq - last) & 0xFFFFFFFF) < 0x80000000


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT

    gp = vg.VX360Gamepad()  # the virtual Xbox 360 controller
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(LINK_TIMEOUT_S)

    print(f"AllyPad receiver: virtual Xbox 360 pad created. "
          f"Listening on UDP port {port}.")
    print("Now start sender_ally.py on the Ally. Press Ctrl+C to stop.")

    last_seq = None
    idle = False

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(64)
            except socket.timeout:
                if not idle:
                    set_neutral(gp)
                    idle = True
                continue

            if len(data) != PACKET_SIZE:
                continue

            seq, buttons, lt, rt, lx, ly, rx, ry = struct.unpack(PACKET_FMT, data)
            if not is_newer(seq, last_seq):
                continue  # drop late or duplicate packets
            last_seq = seq
            idle = False

            r = gp.report
            r.wButtons = buttons
            r.bLeftTrigger = lt
            r.bRightTrigger = rt
            r.sThumbLX = lx
            r.sThumbLY = ly
            r.sThumbRX = rx
            r.sThumbRY = ry
            gp.update()
    except KeyboardInterrupt:
        set_neutral(gp)
        print("\nStopped. Virtual controller removed.")


if __name__ == "__main__":
    main()
