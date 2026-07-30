"""WinPadBridge Receiver v0.5 -- run this ON THE PC you want to control games on.

Bluetooth only (plus optional USB cable). Double-click to open. No terminal,
no firewall, no IPs.

Creates a virtual Xbox 360 controller (via the ViGEmBus driver) and
mirrors whatever the WinPadBridge Sender streams over Bluetooth.
"""

import ctypes
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "WinPadBridge Receiver  (PC)"
BT_CHANNELS = (4, 5, 6, 7)          # RFCOMM channels tried in order
# seq, buttons, LT, RT, LX, LY, RX, RY, touch_flags, touch_dx, touch_dy
PACKET_FMT = "<IHBBhhhhBhh"
PACKET_SIZE = struct.calcsize(PACKET_FMT)
HELLO = b"WINPADBRIDGE?"            # handshake: sender -> receiver
WELCOME = b"WINPADBRIDGE!"          # handshake: receiver -> sender
NO_WINDOW = 0x08000000
AF_BT = getattr(socket, "AF_BLUETOOTH", getattr(socket, "AF_BTH", None))
BT_PROTO = getattr(socket, "BTPROTO_RFCOMM",
                   getattr(socket, "BTHPROTO_RFCOMM", 3))
DISCOVER_MSG = b"WINPADBRIDGE_DISCOVER"  # USB-cable discovery: handheld -> PC
HERE_MSG = b"WINPADBRIDGE_HERE"          # USB-cable discovery: PC -> handheld
CABLE_PORT = 47845
MOUSE_SENSITIVITY = 3.0             # multiplier applied to touchpad drag deltas

# ---------------------------------------------------------------- vgamepad --
VG_ERROR = ""
vg = None
try:
    import vgamepad as vg
except Exception as e:
    VG_ERROR = str(e)

_pad_lock = threading.Lock()


def _axis_to_u8(v):
    """XInput axis (-32768..32767) -> DS4 axis byte (0..255, 128=neutral)."""
    return max(0, min(255, 128 + round((v / 32767.0) * 127)))


class Pad:
    """Wraps the virtual controller so its type can be switched at runtime
    between Xbox 360 (broadest game compatibility, no touchpad) and DS4
    (adds touchpad support, but some Xbox-only games won't see it)."""

    _DPAD = None  # built lazily once vg is known to have loaded
    _BTN_MAP = None

    def __init__(self, pad_type="xbox360"):
        self.type = pad_type
        self.gp = None
        self.error = VG_ERROR
        if vg is not None:
            self._build_maps()
            self._create()

    @classmethod
    def _build_maps(cls):
        if cls._DPAD is not None:
            return
        D = vg.DS4_DPAD_DIRECTIONS
        cls._DPAD = {
            (0, 0): D.DS4_BUTTON_DPAD_NONE,
            (1, 0): D.DS4_BUTTON_DPAD_NORTH,
            (1, 1): D.DS4_BUTTON_DPAD_NORTHEAST,
            (0, 1): D.DS4_BUTTON_DPAD_EAST,
            (-1, 1): D.DS4_BUTTON_DPAD_SOUTHEAST,
            (-1, 0): D.DS4_BUTTON_DPAD_SOUTH,
            (-1, -1): D.DS4_BUTTON_DPAD_SOUTHWEST,
            (0, -1): D.DS4_BUTTON_DPAD_WEST,
            (1, -1): D.DS4_BUTTON_DPAD_NORTHWEST,
        }
        B = vg.DS4_BUTTONS
        # (XInput button bit, matching DS4 button) -- physical face-button
        # positions line up 1:1 between Xbox and PlayStation layouts.
        cls._BTN_MAP = (
            (0x1000, B.DS4_BUTTON_CROSS),           # A
            (0x2000, B.DS4_BUTTON_CIRCLE),           # B
            (0x4000, B.DS4_BUTTON_SQUARE),           # X
            (0x8000, B.DS4_BUTTON_TRIANGLE),         # Y
            (0x0100, B.DS4_BUTTON_SHOULDER_LEFT),    # LB
            (0x0200, B.DS4_BUTTON_SHOULDER_RIGHT),   # RB
            (0x0020, B.DS4_BUTTON_SHARE),            # BACK
            (0x0010, B.DS4_BUTTON_OPTIONS),          # START
            (0x0040, B.DS4_BUTTON_THUMB_LEFT),       # L3
            (0x0080, B.DS4_BUTTON_THUMB_RIGHT),      # R3
        )

    def _create(self):
        try:
            self.gp = vg.VDS4Gamepad() if self.type == "ds4" else vg.VX360Gamepad()
            self.error = ""
        except Exception as e:
            self.gp = None
            self.error = str(e)

    def set_type(self, pad_type):
        if pad_type == self.type and self.gp is not None:
            return
        with _pad_lock:
            self.type = pad_type
            self._create()

    def apply(self, fields):
        """fields = (buttons, lt, rt, lx, ly, rx, ry, touch_flags, touch_x, touch_y)"""
        if self.gp is None:
            return
        with _pad_lock:
            if self.type == "ds4":
                self._apply_ds4(fields)
            else:
                self._apply_xbox360(fields)

    def neutral(self):
        self.apply((0, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    def _apply_xbox360(self, fields):
        buttons, lt, rt, lx, ly, rx, ry = fields[:7]
        r = self.gp.report
        r.wButtons = buttons
        r.bLeftTrigger = lt
        r.bRightTrigger = rt
        r.sThumbLX = lx
        r.sThumbLY = ly
        r.sThumbRX = rx
        r.sThumbRY = ry
        self.gp.update()

    def _apply_ds4(self, fields):
        # Touch fields (index 7-9) aren't part of the DS4 report -- the
        # touchpad rectangle drives the real mouse cursor instead (see
        # MouseCtl), independently of pad type.
        buttons, lt, rt, lx, ly, rx, ry = fields[:7]
        p = self.gp
        p.reset()
        for xinput_bit, ds4_btn in self._BTN_MAP:
            if buttons & xinput_bit:
                p.press_button(ds4_btn)
        updown = (1 if buttons & 0x0001 else 0) - (1 if buttons & 0x0002 else 0)
        leftright = (1 if buttons & 0x0008 else 0) - (1 if buttons & 0x0004 else 0)
        p.directional_pad(self._DPAD[(updown, leftright)])
        p.left_trigger(lt)
        p.right_trigger(rt)
        if lt > 10:
            p.press_button(vg.DS4_BUTTONS.DS4_BUTTON_TRIGGER_LEFT)
        if rt > 10:
            p.press_button(vg.DS4_BUTTONS.DS4_BUTTON_TRIGGER_RIGHT)
        # DS4's Y axis is inverted relative to XInput's (XInput: up = positive).
        p.left_joystick(_axis_to_u8(lx), _axis_to_u8(-ly))
        p.right_joystick(_axis_to_u8(rx), _axis_to_u8(-ry))
        p.update()


PAD = Pad("xbox360")


class MouseCtl:
    """Moves this PC's real mouse cursor from touchpad drag deltas, and
    clicks the left button. Independent of the virtual gamepad/pad type --
    a plain Windows cursor move, like a laptop trackpad."""

    MOVE = 0x0001
    LEFTDOWN = 0x0002
    LEFTUP = 0x0004

    def __init__(self):
        self._user32 = ctypes.windll.user32
        self._clicked = False
        self.last_dx = 0
        self.last_dy = 0
        self.touching = False

    def apply(self, touch_flags, dx, dy):
        touching = bool(touch_flags & 0x01)
        click = bool(touch_flags & 0x02)
        self.touching = touching
        self.last_dx, self.last_dy = dx, dy
        if touching and (dx or dy):
            mx = round(dx * MOUSE_SENSITIVITY)
            my = round(dy * MOUSE_SENSITIVITY)
            if mx or my:
                self._user32.mouse_event(self.MOVE, mx, my, 0, 0)
        if click and not self._clicked:
            self._user32.mouse_event(self.LEFTDOWN, 0, 0, 0, 0)
        elif not click and self._clicked:
            self._user32.mouse_event(self.LEFTUP, 0, 0, 0, 0)
        self._clicked = click


MOUSE = MouseCtl()


def local_bt_mac():
    """Best-effort read of this PC's Bluetooth adapter MAC address."""
    ps = ("Get-NetAdapter | Where-Object "
          "{$_.InterfaceDescription -like '*Bluetooth*'} | "
          "Select-Object -First 1 -ExpandProperty MacAddress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15,
                           creationflags=NO_WINDOW)
        mac = (r.stdout or "").strip().replace("-", ":").lower()
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            return mac
    except Exception:
        pass
    return "unknown"


# -------------------------------------------------------- Bluetooth server --
class BtServer:
    def __init__(self):
        self.stop_ev = threading.Event()
        self.status = "Bluetooth: starting..."
        self.link_active = False
        self.latest = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        self.pps = 0
        self.channel = None
        self.events = []
        threading.Thread(target=self.run, daemon=True).start()

    def _log(self, msg):
        self.events = (self.events + [time.strftime("%H:%M:%S ") + msg])[-6:]

    def stop(self):
        self.stop_ev.set()

    def run(self):
        if AF_BT is None:
            self.status = ("Bluetooth is not supported by this Python "
                           "build. Install Python 3.11+ from python.org.")
            return
        srv = None
        last_err = ""
        any_addr = getattr(socket, "BDADDR_ANY", "00:00:00:00:00:00")
        for ch in BT_CHANNELS:
            try:
                s = socket.socket(AF_BT, socket.SOCK_STREAM,
                                  BT_PROTO)
                s.bind((any_addr, ch))
                s.listen(1)
                srv = s
                self.channel = ch
                break
            except OSError as ex:
                last_err = str(ex)
                try:
                    s.close()
                except OSError:
                    pass
        if srv is None:
            self.status = ("Bluetooth could not start. Turn Bluetooth ON "
                           "in Windows Settings, then press 'Restart "
                           f"Bluetooth'.  ({last_err[:70]})")
            self._log("Failed to start: " + last_err[:70])
            return

        srv.settimeout(1.0)
        self.status = (f"Listening on channel {self.channel} - waiting "
                       "for the sender...")
        self._log(f"Listening on Bluetooth channel {self.channel}")

        while not self.stop_ev.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            peer = addr[0] if isinstance(addr, tuple) else str(addr)
            # Handshake: only accept a real WinPadBridge Sender.
            try:
                conn.settimeout(3)
                greet = b""
                while len(greet) < len(HELLO):
                    part = conn.recv(len(HELLO) - len(greet))
                    if not part:
                        raise OSError("closed")
                    greet += part
                if greet != HELLO:
                    raise OSError("not a WinPadBridge sender")
                conn.sendall(WELCOME)
            except OSError:
                self._log(f"Rejected a non-WinPadBridge connection ({peer})")
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            self._log(f"Sender connected ({peer})")
            self.status = f"CONNECTED  ({peer})"
            self.link_active = True
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            conn.settimeout(0.5)
            buf = b""
            last_data = time.monotonic()
            count = 0
            t0 = time.monotonic()

            while not self.stop_ev.is_set():
                try:
                    chunk = conn.recv(512)
                    if not chunk:
                        break                       # sender disconnected
                    buf += chunk
                    last_data = time.monotonic()
                except socket.timeout:
                    if time.monotonic() - last_data > 0.8:
                        PAD.neutral()               # stalled link safety
                        MOUSE.apply(0, 0, 0)
                except OSError:
                    break
                else:
                    while len(buf) >= PACKET_SIZE:
                        pkt = buf[:PACKET_SIZE]
                        buf = buf[PACKET_SIZE:]
                        # if more full packets are already queued behind
                        # this one, skip straight to the newest - never
                        # play back a backlog in slow motion
                        if len(buf) >= PACKET_SIZE:
                            continue
                        _seq, *fields = struct.unpack(PACKET_FMT, pkt)
                        self.latest = tuple(fields)
                        PAD.apply(self.latest)
                        MOUSE.apply(*self.latest[7:10])
                        count += 1

                now = time.monotonic()
                if now - t0 >= 1.0:
                    self.pps = count
                    count = 0
                    t0 = now

            try:
                conn.close()
            except OSError:
                pass
            self.link_active = False
            self.latest = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            PAD.neutral()
            MOUSE.apply(0, 0, 0)
            self._log("Sender disconnected")
            self.status = (f"Listening on channel {self.channel} - waiting "
                           "for the sender...")

        srv.close()


class CableServer:
    """Listens for the sender over a USB data-link cable (it enumerates as a
    normal network adapter). Uses UDP + answers discovery pings so the
    sender can auto-find this PC's link-local IP."""

    def __init__(self):
        self.stop_ev = threading.Event()
        self.status = "USB cable: waiting for the sender..."
        self.link_active = False
        self.latest = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        self.pps = 0
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", CABLE_PORT))
        except OSError as e:
            self.status = f"USB cable: cannot listen on port {CABLE_PORT}: {e}"
            return
        sock.settimeout(0.5)
        last_seq = None
        count = 0
        t0 = time.monotonic()

        while not self.stop_ev.is_set():
            try:
                data, addr = sock.recvfrom(64)
            except socket.timeout:
                if self.link_active:
                    self.link_active = False
                    self.latest = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
                    PAD.neutral()
                    MOUSE.apply(0, 0, 0)
                data = None
            except OSError:
                break

            now = time.monotonic()
            if now - t0 >= 1.0:
                self.pps = count
                count = 0
                t0 = now
            if not data:
                continue
            if data == DISCOVER_MSG:
                try:
                    sock.sendto(HERE_MSG, addr)
                except OSError:
                    pass
                continue
            if len(data) != PACKET_SIZE:
                continue

            seq, *fields = struct.unpack(PACKET_FMT, data)
            if last_seq is not None and seq == last_seq:
                continue
            last_seq = seq
            count += 1
            self.latest = tuple(fields)
            self.link_active = True
            self.status = f"USB cable: CONNECTED ({addr[0]})"
            PAD.apply(self.latest)
            MOUSE.apply(*self.latest[7:10])

        sock.close()
        PAD.neutral()
        MOUSE.apply(0, 0, 0)


# --------------------------------------------------------- controller view --
BTN = {"DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004,
       "DPAD_RIGHT": 0x0008, "START": 0x0010, "BACK": 0x0020,
       "L3": 0x0040, "R3": 0x0080, "LB": 0x0100, "RB": 0x0200,
       "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000}

PRESSED = {"A": "#43a047", "B": "#e53935", "X": "#1e88e5", "Y": "#fdd835"}


class ControllerView(tk.Canvas):
    W, H = 540, 300

    def __init__(self, master):
        super().__init__(master, width=self.W, height=self.H,
                         bg="#15161a", highlightthickness=0)
        self.i = {}
        self._build()

    def _rect(self, name, x1, y1, x2, y2, label):
        self.i[name] = self.create_rectangle(x1, y1, x2, y2,
                                             outline="#777", width=2, fill="")
        self.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=label,
                         fill="#ccc", font=("Segoe UI", 9, "bold"))

    def _build(self):
        c = self
        c.create_rectangle(40, 20, 62, 100, outline="#777", width=2)
        self.i["LTf"] = c.create_rectangle(42, 98, 60, 98, fill="#4caf50", width=0)
        c.create_rectangle(478, 20, 500, 100, outline="#777", width=2)
        self.i["RTf"] = c.create_rectangle(480, 98, 498, 98, fill="#4caf50", width=0)
        c.create_text(51, 112, text="LT", fill="#999")
        c.create_text(489, 112, text="RT", fill="#999")
        self._rect("LB", 85, 25, 160, 52, "LB")
        self._rect("RB", 380, 25, 455, 52, "RB")
        self._rect("BACK", 210, 30, 255, 52, "BACK")
        self._rect("START", 285, 30, 335, 52, "START")
        self.i["Lring"] = c.create_oval(90, 105, 180, 195, outline="#777", width=2)
        self.i["Ldot"] = c.create_oval(127, 142, 143, 158, fill="#4caf50", width=0)
        self.i["Rring"] = c.create_oval(360, 105, 450, 195, outline="#777", width=2)
        self.i["Rdot"] = c.create_oval(397, 142, 413, 158, fill="#4caf50", width=0)
        c.create_text(135, 205, text="L stick", fill="#999")
        c.create_text(405, 205, text="R stick", fill="#999")
        self._rect("DPAD_UP", 215, 118, 240, 143, "▲")
        self._rect("DPAD_DOWN", 215, 173, 240, 198, "▼")
        self._rect("DPAD_LEFT", 190, 145, 215, 170, "◀")
        self._rect("DPAD_RIGHT", 240, 145, 265, 170, "▶")
        for name, (cx, cy) in {"Y": (315, 122), "X": (289, 148),
                               "B": (341, 148), "A": (315, 174)}.items():
            self.i[name] = c.create_oval(cx - 13, cy - 13, cx + 13, cy + 13,
                                         outline="#777", width=2, fill="")
            c.create_text(cx, cy, text=name, fill="#ccc",
                          font=("Segoe UI", 9, "bold"))
        self.i["txt"] = c.create_text(self.W / 2, 235, fill="#8bc34a",
                                      font=("Consolas", 10), text="")
        self.i["txt2"] = c.create_text(self.W / 2, 258, fill="#8bc34a",
                                       font=("Consolas", 10), text="")
        self.i["msg"] = c.create_text(self.W / 2, 282, fill="#bbb",
                                      font=("Segoe UI", 9), text="")

    def show(self, state, msg=""):
        buttons, lt, rt, lx, ly, rx, ry = state
        for name, mask in BTN.items():
            if name in ("L3", "R3"):
                continue
            on = bool(buttons & mask)
            fill = PRESSED.get(name, "#e0a400") if on else ""
            self.itemconfig(self.i[name], fill=fill)
        self.itemconfig(self.i["Lring"],
                        outline="#e0a400" if buttons & BTN["L3"] else "#777")
        self.itemconfig(self.i["Rring"],
                        outline="#e0a400" if buttons & BTN["R3"] else "#777")
        for dot, cx, cy, vx, vy in (("Ldot", 135, 150, lx, ly),
                                    ("Rdot", 405, 150, rx, ry)):
            x = cx + (vx / 32768.0) * 34
            y = cy - (vy / 32768.0) * 34
            self.coords(self.i[dot], x - 8, y - 8, x + 8, y + 8)
        self.coords(self.i["LTf"], 42, 98 - (lt / 255.0) * 76, 60, 98)
        self.coords(self.i["RTf"], 480, 98 - (rt / 255.0) * 76, 498, 98)
        self.itemconfig(self.i["txt"],
                        text=f"LX {lx:>6}  LY {ly:>6}   RX {rx:>6}  RY {ry:>6}")
        self.itemconfig(self.i["txt2"],
                        text=f"LT {lt:>3}  RT {rt:>3}   buttons 0x{buttons:04X}")
        self.itemconfig(self.i["msg"], text=msg)


class TouchMonitor(tk.Canvas):
    """Visual proof that touchpad packets are arriving: a dot that walks
    around this box by the same drag deltas being sent to the real mouse
    cursor. Doesn't reflect actual cursor position -- it's a diagnostic,
    not a mirror of the screen."""

    W, H = 300, 160

    def __init__(self, master):
        super().__init__(master, width=self.W, height=self.H,
                         bg="#15161a", highlightthickness=0)
        self.x = self.W / 2
        self.y = self.H / 2
        self.dot = self.create_oval(0, 0, 0, 0, fill="#666", outline="")
        self.txt = self.create_text(self.W / 2, self.H - 14, fill="#999",
                                    font=("Consolas", 9), text="")
        self._place()

    def _place(self):
        r = 10
        self.coords(self.dot, self.x - r, self.y - r, self.x + r, self.y + r)

    def show(self, touching, click, dx, dy):
        if touching:
            self.x = max(10, min(self.W - 10, self.x + dx))
            self.y = max(10, min(self.H - 10, self.y + dy))
            fill = "#ffb02e" if click else "#43d167"
        else:
            fill = "#666"
        self._place()
        self.itemconfig(self.dot, fill=fill)
        state = "touching" if touching else "idle"
        if click:
            state += " + click"
        self.itemconfig(self.txt, text=f"{state}   dx {dx:>3} dy {dy:>3}")


# -------------------------------------------------------------------- app ---
HELP_TEXT = """SIMPLE STEPS

ONE TIME ONLY
1. Install the ViGEmBus driver:
   github.com/nefarius/ViGEmBus/releases -> ViGEmBus_x64.exe
2. If a yellow warning shows here, click "Install vgamepad"
   and then close and reopen this app.
3. Pair the two devices: this PC's Settings > Bluetooth ON
   (keep the page open). Handheld's Settings > Bluetooth >
   Add device > pick this PC > accept the PIN on BOTH.

EVERY TIME
1. Open this app on this PC. It waits on Bluetooth.
2. On the handheld: switch controls to GAMEPAD mode
   (Command Center button, Armoury Crate, or your device's
   equivalent > Control Mode > Gamepad).
3. Open WinPadBridge Sender on the handheld > Scan > pick
   this PC > START.
4. This window turns green: CONNECTED. Then in your game or
   emulator, e.g. PCSX2: Settings > Controllers > Controller
   Port 1 > Automatic Mapping > pick the Xbox 360 controller.

IF CONNECTED BUT NOTHING MOVES
The handheld is in Desktop/Mouse mode (its mouse moves when
you touch the sticks). Fix it on the handheld: Command
Center button (or equivalent) > Control Mode > GAMEPAD. The
sender screen shows this in red and turns green when fixed.

No firewall setup is needed - Bluetooth is not affected
by Windows Firewall.

USB CABLE - ALTERNATIVE METHOD
Needs a USB-C DATA-LINK cable (a "PC-to-PC transfer
cable"), not a plain charging cable. Once plugged in,
Windows shows a new network adapter on both devices - no
extra setup needed here, this app listens for it
automatically. On the handheld, choose "USB cable" instead
of Bluetooth and press START.

TOUCHPAD
The sender has a drag area plus a click button. Dragging it
moves this PC's real mouse cursor directly (like a laptop
trackpad) - it works no matter which Pad type is selected,
since it doesn't go through the virtual controller at all.
The "Touchpad monitor" box on the Main tab shows a dot so
you can confirm packets are arriving even with no mouse
movement in view.

PAD TYPE (Xbox 360 / DS4)
This only affects buttons/sticks/triggers. Xbox 360 is the
default (broadest game compatibility). DS4 makes this PC's
virtual controller show up as a DualShock 4 instead - some
PlayStation-native ports expect that, but some Xbox-only
games won't recognize it, so only switch if you need it."""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.resizable(False, False)
        self.bt = BtServer()
        self.cable = CableServer()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_main = ttk.Frame(nb)
        self.tab_help = ttk.Frame(nb)
        nb.add(self.tab_main, text="   Main   ")
        nb.add(self.tab_help, text="   Help   ")

        f = self.tab_main
        self.lbl_pad = tk.Label(f, font=("Segoe UI", 12, "bold"))
        self.lbl_pad.pack(anchor="w", padx=12, pady=(10, 2))

        if PAD.gp is None:
            warn = tk.Frame(f, bg="#fff3cd", bd=1, relief="solid")
            warn.pack(fill="x", padx=12, pady=4)
            tk.Label(warn, bg="#fff3cd", justify="left", wraplength=430,
                     text=("vgamepad / ViGEmBus missing - games cannot see "
                           "a controller yet.\n1) Install ViGEmBus driver "
                           "(Help tab)  2) Click Install, then reopen.\n"
                           f"Details: {VG_ERROR[:110]}")
                     ).pack(side="left", padx=8, pady=6)
            tk.Button(warn, text="Install vgamepad",
                      command=self._install_vgamepad).pack(side="right",
                                                           padx=8, pady=6)

        prow = tk.Frame(f)
        prow.pack(anchor="w", padx=12, pady=(0, 6))
        tk.Label(prow, text="Pad type:").pack(side="left")
        self.pad_type = tk.StringVar(value="xbox360")
        for val, label in (("xbox360", "Xbox 360"), ("ds4", "DS4")):
            tk.Radiobutton(prow, text=label, variable=self.pad_type, value=val,
                          command=self._on_pad_type_change)\
                .pack(side="left", padx=(8, 0))
        tk.Label(prow, fg="#888",
                 text="  (buttons/sticks only; some Xbox-only games won't "
                      "recognize a DS4 pad. Touchpad works either way.)")\
            .pack(side="left")

        self.lbl_btid = tk.Label(f, font=("Consolas", 10), fg="#555")
        self.lbl_btid.pack(anchor="w", padx=12)
        brow = tk.Frame(f)
        brow.pack(fill="x", padx=12, pady=6)
        self.lbl_bt = tk.Label(brow, font=("Segoe UI", 13, "bold"),
                               justify="left", wraplength=420)
        self.lbl_bt.pack(side="left")
        tk.Button(brow, text="Restart Bluetooth",
                  command=self._restart_bt).pack(side="right")
        self.lbl_log = tk.Label(f, font=("Consolas", 9), fg="#777",
                                justify="left")
        self.lbl_log.pack(anchor="w", padx=12)

        crow = tk.Frame(f)
        crow.pack(fill="x", padx=12, pady=(2, 6))
        self.lbl_cable = tk.Label(crow, font=("Segoe UI", 13, "bold"),
                                  justify="left", wraplength=420)
        self.lbl_cable.pack(side="left")
        tk.Button(crow, text="Restart USB cable",
                  command=self._restart_cable).pack(side="right")

        self.view = ControllerView(f)
        self.view.pack(padx=12, pady=6)

        tk.Label(f, fg="#9aa0a6",
                 text="Touchpad monitor (moves this PC's real mouse "
                      "cursor too):").pack(anchor="w", padx=12, pady=(4, 0))
        self.touch_mon = TouchMonitor(f)
        self.touch_mon.pack(padx=12, pady=(2, 6))

        tk.Label(f, fg="#666", justify="left",
                 text="While this window is open, games see a normal "
                      "Xbox 360 (or DS4) controller.\nExample (PCSX2): "
                      "Settings > Controllers > Port 1 > Automatic Mapping.")\
            .pack(anchor="w", padx=12, pady=(0, 10))

        txt = tk.Text(self.tab_help, wrap="word", width=62, height=30,
                      font=("Consolas", 9))
        txt.insert("1.0", HELP_TEXT)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True, padx=8, pady=8)

        self.lbl_btid.config(text="This PC over Bluetooth:  reading...")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(200, self._late_init)
        self.after(33, self._tick)

    def _late_init(self):
        def work():
            mac = local_bt_mac()
            name = socket.gethostname()
            self.after(0, lambda: self.lbl_btid.config(
                text=f"This PC over Bluetooth:  {name}   [{mac}]"))
        threading.Thread(target=work, daemon=True).start()

    def _install_vgamepad(self):
        if not messagebox.askyesno(APP_NAME,
                                   "Install the vgamepad package now?\n"
                                   "(A driver installer window may pop up - "
                                   "accept it.)"):
            return

        def work():
            r = subprocess.run([sys.executable, "-m", "pip",
                                "install", "vgamepad"],
                               capture_output=True, text=True)
            ok = r.returncode == 0
            msg = ("Installed! Please close and reopen this app."
                   if ok else "Install failed:\n" + (r.stderr or "")[-400:])
            self.after(0, lambda: messagebox.showinfo(APP_NAME, msg))
        threading.Thread(target=work, daemon=True).start()

    def _restart_bt(self):
        self.bt.stop()
        self.bt = BtServer()

    def _restart_cable(self):
        self.cable.stop()
        self.cable = CableServer()

    def _on_pad_type_change(self):
        PAD.set_type(self.pad_type.get())

    def _tick(self):
        if PAD.gp is not None:
            label = "DS4" if PAD.type == "ds4" else "Xbox 360"
            self.lbl_pad.config(text=f"Virtual {label} pad:  ACTIVE ✔",
                                fg="#2e7d32")
        else:
            self.lbl_pad.config(text=f"Virtual pad:  NOT ACTIVE "
                                     f"({PAD.error[:60]})", fg="#c62828")
        bt = self.bt
        color = "#2e7d32" if bt.link_active else "#e65100"
        extra = f"   -   {bt.pps} packets/s" if bt.link_active else ""
        self.lbl_bt.config(text="Bluetooth:  " + bt.status + extra, fg=color)
        self.lbl_log.config(text="\n".join(bt.events))

        cab = self.cable
        ccolor = "#2e7d32" if cab.link_active else "#e65100"
        cextra = f"   -   {cab.pps} packets/s" if cab.link_active else ""
        self.lbl_cable.config(text=cab.status + cextra, fg=ccolor)

        if bt.link_active:
            full, msg = bt.latest, "live over Bluetooth"
        elif cab.link_active:
            full, msg = cab.latest, "live over USB cable"
        else:
            full, msg = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0), "no data arriving"
        self.view.show(full[:7], msg)

        touch_flags, dx, dy = full[7:10]
        self.touch_mon.show(bool(touch_flags & 0x01), bool(touch_flags & 0x02),
                            dx, dy)
        self.after(33, self._tick)

    def _close(self):
        self.bt.stop()
        self.cable.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
