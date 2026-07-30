"""WinPadBridge sender -- run this ON THE HANDHELD (or any Windows PC).

Reads a connected Windows XInput gamepad (built-in on handhelds like the
ROG Ally, Legion Go, GPD Win, or AYANEO) and streams its state over UDP
to the receiver running on the other PC.

Usage:
    python sender.py <receiver-ip> [port]

Example (receiver PC running Windows Mobile Hotspot):
    python sender.py 192.168.137.1

No extra packages needed -- Python 3.11+ standard library only.
"""

import ctypes
import socket
import struct
import sys
import time

DEFAULT_PORT = 47845
RATE_HZ = 250                      # packets per second
PACKET_FMT = "<IHBBhhhh"           # seq, buttons, LT, RT, LX, LY, RX, RY


class XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XInputState(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint),
        ("Gamepad", XInputGamepad),
    ]


def load_xinput():
    """Load the newest XInput DLL available on this Windows machine."""
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, name)
        except OSError:
            continue
    raise RuntimeError("No XInput DLL found. This script must run on Windows.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    target_ip = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    xinput = load_xinput()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    state = XInputState()
    period = 1.0 / RATE_HZ
    seq = 0
    warned = False

    print(f"WinPadBridge sender: streaming controller 0 -> {target_ip}:{port} "
          f"at {RATE_HZ} Hz. Press Ctrl+C to stop.")

    try:
        while True:
            result = xinput.XInputGetState(0, ctypes.byref(state))
            if result == 0:  # ERROR_SUCCESS
                g = state.Gamepad
                seq = (seq + 1) & 0xFFFFFFFF
                packet = struct.pack(
                    PACKET_FMT, seq,
                    g.wButtons, g.bLeftTrigger, g.bRightTrigger,
                    g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY,
                )
                sock.sendto(packet, (target_ip, port))
                warned = False
                time.sleep(period)
            else:
                if not warned:
                    print("Controller 0 not detected. Make sure your handheld's "
                          "controls are in Gamepad/XInput mode. Retrying...")
                    warned = True
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
