"""WinPadBridge Sender v0.5 -- run this ON THE HANDHELD.

Bluetooth only (plus optional USB cable). Fullscreen. Exit ONLY with the
red EXIT button (touch).

Why fullscreen?  In Desktop control mode many Windows handhelds turn the
right stick into a mouse and LT into Shift+Tab, which used to throw you
out of the window. Fullscreen + always-on-top means stray clicks land
here and nothing can steal the screen while you verify.
"""

import ctypes
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "WinPadBridge Sender"
BT_CHANNELS = (4, 5, 6, 7)              # RFCOMM channels tried in order
# seq, buttons, LT, RT, LX, LY, RX, RY, touch_flags, touch_dx, touch_dy
PACKET_FMT = "<IHBBhhhhBhh"
HELLO = b"WINPADBRIDGE?"                # handshake: sender -> receiver
WELCOME = b"WINPADBRIDGE!"              # handshake: receiver -> sender
DISCOVER_MSG = b"WINPADBRIDGE_DISCOVER"  # USB-cable discovery: sender -> PC
HERE_MSG = b"WINPADBRIDGE_HERE"          # USB-cable discovery: PC -> sender
CABLE_PORT = 47845


def discover_over_cable(timeout=2.5):
    """Find the receiver PC over a USB data-link cable (it enumerates as a
    normal network adapter, usually with a 169.254.x.x link-local address).
    Broadcasts on every local network interface and returns the first
    reply."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.4)
    targets = {("255.255.255.255", CABLE_PORT)}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            net = ip.rsplit(".", 1)[0]
            targets.add((net + ".255", CABLE_PORT))
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            for t in targets:
                try:
                    s.sendto(DISCOVER_MSG, t)
                except OSError:
                    pass
            try:
                data, addr = s.recvfrom(64)
                if data == HERE_MSG:
                    return addr[0]
            except socket.timeout:
                continue
    finally:
        s.close()
    return None
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".winpadbridge_sender.json")
NO_WINDOW = 0x08000000
AF_BT = getattr(socket, "AF_BLUETOOTH", getattr(socket, "AF_BTH", None))
BT_PROTO = getattr(socket, "BTPROTO_RFCOMM",
                   getattr(socket, "BTHPROTO_RFCOMM", 3))

BG = "#101216"
FG = "#e8e8e8"
GOOD = "#43d167"
BAD = "#ff5a52"
WARN = "#ffb02e"


# ----------------------------------------------------------------- XInput ---
class XInputGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint),
                ("Gamepad", XInputGamepad)]


def load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, name)
        except OSError:
            continue
    raise RuntimeError("XInput not found (this must run on Windows.")


# ---------------------------------------------------- Bluetooth device scan --
def list_paired_bluetooth():
    """Return [(name, mac)] of Bluetooth devices paired with this device."""
    ps = ("Get-PnpDevice -Class Bluetooth | "
          "Where-Object {$_.InstanceId -like 'BTHENUM\\DEV_*'} | "
          "Select-Object FriendlyName, InstanceId | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
        raw = (r.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
    except Exception:
        return []
    seen = {}
    for item in data:
        iid = item.get("InstanceId", "") or ""
        name = item.get("FriendlyName", "") or "Unknown device"
        m = re.search(r"DEV_([0-9A-Fa-f]{12})", iid)
        if not m:
            continue
        h = m.group(1).lower()
        mac = ":".join(h[i:i + 2] for i in range(0, 12, 2))
        seen.setdefault(mac, name)
    return [(name, mac) for mac, name in seen.items()]


# --------------------------------------------------------------- gamepad ----
class PadEngine:
    """Polls the handheld's gamepad on all 4 XInput slots, tracks activity."""

    def __init__(self):
        self.stop_ev = threading.Event()
        self.controller_ok = False
        self.slot = -1
        self.activity = False
        self._last_pkt = None
        self._last_change = 0.0
        self.latest = (0, 0, 0, 0, 0, 0, 0)
        self.error = ""
        self.changed = threading.Event()
        # touchpad state, driven by the UI thread (see TouchPad widget).
        # dx/dy accumulate drag movement since the last time a streamer
        # thread packed and sent it (see take_touch_delta).
        self.touch_active = False
        self.touch_dx = 0
        self.touch_dy = 0
        self.touch_click = False
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    def set_touch(self, active):
        self.touch_active = active
        self.changed.set()

    def add_touch_delta(self, dx, dy):
        self.touch_dx += dx
        self.touch_dy += dy
        self.changed.set()

    def take_touch_delta(self):
        dx, dy = self.touch_dx, self.touch_dy
        self.touch_dx = 0
        self.touch_dy = 0
        return dx, dy

    def set_touch_click(self, click):
        self.touch_click = click
        self.changed.set()

    @property
    def touch_flags(self):
        return (1 if self.touch_active else 0) | (2 if self.touch_click else 0)

    def run(self):
        try:
            xinput = load_xinput()
        except Exception as e:
            self.error = str(e)
            return
        state = XInputState()
        while not self.stop_ev.is_set():
            slot = -1
            for i in range(4):
                if xinput.XInputGetState(i, ctypes.byref(state)) == 0:
                    slot = i
                    break
            ok = slot >= 0
            self.controller_ok = ok
            self.slot = slot
            now = time.monotonic()
            if ok:
                if state.dwPacketNumber != self._last_pkt:
                    self._last_pkt = state.dwPacketNumber
                    self._last_change = now
                self.activity = (now - self._last_change) < 2.0
                g = state.Gamepad
                new_state = (g.wButtons, g.bLeftTrigger, g.bRightTrigger,
                            g.sThumbLX, g.sThumbLY,
                            g.sThumbRX, g.sThumbRY)
                if new_state != self.latest:
                    self.latest = new_state
                    self.changed.set()
                time.sleep(0.001)
            else:
                self.activity = False
                self.latest = (0, 0, 0, 0, 0, 0, 0)
                time.sleep(0.3)


class BtStreamer(threading.Thread):
    """Connects to the receiver PC over Bluetooth RFCOMM and streams the pad."""

    def __init__(self, engine, mac):
        super().__init__(daemon=True)
        self.engine = engine
        self.mac = mac
        self.stop_ev = threading.Event()
        self.status = "Connecting over Bluetooth..."
        self.connected = False
        self.pps = 0
        self.detail = ""

    @staticmethod
    def _diagnose(errs):
        codes = set(errs.values())
        text = " ".join(str(c) for c in codes)
        if 10061 in codes:
            return ("PC reached, but the Receiver app is not "
                    "answering. Open (or reopen) WinPadBridge Receiver on "
                    "that PC - it must say 'Listening on channel...'. "
                    "Retrying...")
        if "wrong service" in text or "handshake" in text:
            return ("Another Bluetooth service answered instead of "
                    "WinPadBridge. Open the Receiver on that PC - I will "
                    "find it automatically. Retrying...")
        if codes & {10060, 10064, 10065}:
            return ("No answer from that device. Check: is this the right "
                    "PC?  Bluetooth ON on both?  Paired in Windows "
                    "Settings?  Retrying...")
        if codes & {10013, 5}:
            return ("Access denied - the two devices are not paired. "
                    "Pair them in Windows Settings first. Retrying...")
        return f"Bluetooth connect failed. Retrying...  ({text[:80]})"

    def stop(self):
        self.stop_ev.set()

    def run(self):
        if AF_BT is None:
            self.status = ("This Python build has no Bluetooth support. "
                           "Install Python 3.11+ from python.org.")
            return
        while not self.stop_ev.is_set():
            sock = None
            errs = {}
            for ch in BT_CHANNELS:
                if self.stop_ev.is_set():
                    return
                s = None
                try:
                    s = socket.socket(AF_BT, socket.SOCK_STREAM,
                                      BT_PROTO)
                    s.settimeout(8)
                    self.status = f"Trying PC on channel {ch}..."
                    s.connect((self.mac, ch))
                    try:
                        s.setsockopt(socket.IPPROTO_TCP,
                                     socket.TCP_NODELAY, 1)
                    except OSError:
                        pass
                    # Handshake: make sure it's OUR receiver, not some
                    # other Windows Bluetooth service on this channel.
                    s.settimeout(3)
                    s.sendall(HELLO)
                    resp = b""
                    while len(resp) < len(WELCOME):
                        part = s.recv(len(WELCOME) - len(resp))
                        if not part:
                            raise OSError("closed during handshake")
                        resp += part
                    if resp != WELCOME:
                        raise OSError("wrong service on this channel")
                    sock = s
                    break
                except OSError as ex:
                    errs[ch] = getattr(ex, "winerror", None) or str(ex)
                    if s is not None:
                        try:
                            s.close()
                        except OSError:
                            pass
            if sock is None:
                self.connected = False
                self.status = self._diagnose(errs)
                self.detail = "  ".join(f"ch{c}: {e}"
                                        for c, e in errs.items())
                if self.stop_ev.wait(3):
                    return
                continue

            self.connected = True
            self.status = "Bluetooth CONNECTED"
            self.detail = ""
            sock.settimeout(4)
            seq = 0
            count = 0
            t0 = time.monotonic()
            last_sent = 0.0
            MIN_GAP = 1.0 / 125          # hard cap: never flood the link
            try:
                while not self.stop_ev.is_set():
                    self.engine.changed.wait(timeout=0.1)
                    self.engine.changed.clear()
                    now = time.monotonic()
                    if now - last_sent < MIN_GAP:
                        time.sleep(MIN_GAP - (now - last_sent))
                        now = time.monotonic()
                    last_sent = now
                    seq = (seq + 1) & 0xFFFFFFFF
                    e = self.engine
                    dx, dy = e.take_touch_delta()
                    sock.sendall(struct.pack(PACKET_FMT, seq, *e.latest,
                                             e.touch_flags, dx, dy))
                    count += 1
                    if now - t0 >= 1.0:
                        self.pps = count
                        count = 0
                        t0 = now
            except OSError:
                self.connected = False
                self.status = "Connection lost - reconnecting..."
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            if self.stop_ev.wait(2):
                return
        self.connected = False


class CableStreamer(threading.Thread):
    """Streams over a USB data-link cable (shows up as a network adapter)
    using UDP to the receiver PC's IP."""

    def __init__(self, engine, ip):
        super().__init__(daemon=True)
        self.engine = engine
        self.ip = ip
        self.stop_ev = threading.Event()
        self.status = "Connecting over USB cable..."
        self.connected = False
        self.pps = 0
        self.detail = ""

    def stop(self):
        self.stop_ev.set()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq = 0
        count = 0
        t0 = time.monotonic()
        last_sent = 0.0
        MIN_GAP = 1.0 / 200          # cable can handle a faster cap than BT
        self.connected = True
        self.status = f"USB cable: streaming to {self.ip}"
        try:
            while not self.stop_ev.is_set():
                self.engine.changed.wait(timeout=0.1)
                self.engine.changed.clear()
                now = time.monotonic()
                if now - last_sent < MIN_GAP:
                    time.sleep(MIN_GAP - (now - last_sent))
                    now = time.monotonic()
                last_sent = now
                seq = (seq + 1) & 0xFFFFFFFF
                e = self.engine
                dx, dy = e.take_touch_delta()
                try:
                    sock.sendto(struct.pack(PACKET_FMT, seq, *e.latest,
                                            e.touch_flags, dx, dy),
                               (self.ip, CABLE_PORT))
                except OSError as e:
                    self.status = f"USB cable send failed: {e}"
                    self.connected = False
                count += 1
                if now - t0 >= 1.0:
                    self.pps = count
                    count = 0
                    t0 = now
        finally:
            sock.close()
        self.connected = False


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


class TouchPad(tk.Canvas):
    """A drag rectangle that works like a laptop trackpad: dragging sends
    relative motion deltas that move the receiver PC's real mouse cursor.
    The dot only shows where you're currently touching on THIS rectangle,
    not where the remote cursor ends up."""

    W, H = 460, 226

    def __init__(self, master, engine):
        super().__init__(master, width=self.W, height=self.H,
                         bg="#1b1d22", highlightthickness=2,
                         highlightbackground="#444")
        self.engine = engine
        self._last = None
        self.hint = self.create_text(self.W / 2, self.H / 2,
                                     text="touchpad - drag here", fill="#555",
                                     font=("Segoe UI", 11))
        self.dot = self.create_oval(0, 0, 0, 0, fill="", outline="")
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._up)

    def _down(self, ev):
        self._last = (ev.x, ev.y)
        self.engine.set_touch(True)
        self._draw(ev.x, ev.y)
        self.itemconfig(self.hint, text="")

    def _move(self, ev):
        if self._last is None:
            self._last = (ev.x, ev.y)
            return
        dx = ev.x - self._last[0]
        dy = ev.y - self._last[1]
        self._last = (ev.x, ev.y)
        self.engine.add_touch_delta(dx, dy)
        self._draw(ev.x, ev.y)

    def _up(self, _ev):
        self._last = None
        self.engine.set_touch(False)
        self.itemconfig(self.dot, fill="", outline="")
        self.itemconfig(self.hint, text="touchpad - drag here")

    def _draw(self, x, y):
        x = max(0, min(self.W, x))
        y = max(0, min(self.H, y))
        r = 9
        self.coords(self.dot, x - r, y - r, x + r, y + r)
        self.itemconfig(self.dot, fill="#43d167", outline="#2e7d32")


# -------------------------------------------------------------------- app ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 14))

        self.engine = PadEngine()
        self.bt = None
        self.cable = None
        self.bt_devices = []

        # ---- top bar with the ONLY exit
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=20, pady=(14, 4))
        tk.Label(top, text="WinPadBridge Sender", bg=BG, fg=FG,
                 font=("Segoe UI", 20, "bold")).pack(side="left")
        tk.Button(top, text="  ✕  EXIT  ", bg="#c62828", fg="white",
                  font=("Segoe UI", 16, "bold"), bd=0, takefocus=0,
                  activebackground="#e53935", activeforeground="white",
                  command=self._close).pack(side="right", ipadx=8, ipady=6)
        tk.Label(top, text="(touch here to close - buttons can't close it)",
                 bg=BG, fg="#888", font=("Segoe UI", 10)).pack(side="right",
                                                               padx=12)

        # Scrollable body: some handhelds run this fullscreen window at a
        # logical resolution shorter than the content needs (e.g. the ROG
        # Ally X commonly runs at 150% display scaling, reporting a ~720px
        # tall screen). A canvas + scrollbar keeps everything reachable
        # instead of silently clipping the bottom of the layout.
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", width=28,
                                 command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=BG)
        body_window = canvas.create_window((0, 0), window=body, anchor="n")

        def _on_body_configure(_ev):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(ev):
            canvas.coords(body_window, ev.width / 2, 0)

        body.bind("<Configure>", _on_body_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(ev):
            canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        tk.Label(body, bg=BG, fg="#9aa0a6", font=("Segoe UI", 12),
                 text="One-time: pair Bluetooth in Windows Settings, or "
                      "plug in a USB data-link cable."
                 ).pack(pady=(0, 8))

        self.mode = tk.StringVar(value="bt")
        mrow = tk.Frame(body, bg=BG)
        mrow.pack(pady=2)
        for val, label in (("bt", "Bluetooth"), ("cable", "USB cable")):
            tk.Radiobutton(mrow, text=label, variable=self.mode, value=val,
                          bg=BG, fg=FG, selectcolor="#2b2f36",
                          activebackground=BG, activeforeground=FG,
                          font=("Segoe UI", 13, "bold"), takefocus=0,
                          command=self._on_mode_change)\
                .pack(side="left", padx=10)

        self.row_bt = tk.Frame(body, bg=BG)
        self.btn_scan = tk.Button(self.row_bt, text="Scan paired devices",
                                  font=("Segoe UI", 14, "bold"),
                                  takefocus=0, command=self._scan_bt,
                                  bd=0, bg="#2b2f36", fg=FG,
                                  activebackground="#3a4048",
                                  activeforeground=FG)
        self.btn_scan.pack(side="left", ipadx=14, ipady=10, padx=8)
        self.bt_combo = ttk.Combobox(self.row_bt, width=28,
                                     font=("Segoe UI", 14))
        self.bt_combo.pack(side="left", padx=8, ipady=6)

        self.row_cable = tk.Frame(body, bg=BG)
        tk.Label(self.row_cable, text="Just plug in the USB cable and "
                 "press START - the receiver PC is found automatically.",
                 bg=BG, fg="#9aa0a6", font=("Segoe UI", 12))\
            .pack(side="left", padx=8)
        self.cable_ip = tk.StringVar()
        tk.Label(self.row_cable, textvariable=self.cable_ip, bg=BG,
                 fg="#6a6f78", font=("Consolas", 11)).pack(side="left",
                                                            padx=8)

        self.row_bt.pack(pady=4)

        self.btn_start = tk.Button(body, text="▶   START STREAMING",
                                   font=("Segoe UI", 20, "bold"),
                                   bg="#2e7d32", fg="white", bd=0,
                                   takefocus=0, command=self._toggle,
                                   activebackground="#37944d",
                                   activeforeground="white")
        self.btn_start.pack(pady=14, ipadx=30, ipady=14)

        self.lbl_ctrl = tk.Label(body, bg=BG, font=("Segoe UI", 15, "bold"))
        self.lbl_ctrl.pack()
        self.lbl_mode = tk.Label(body, bg=BG, font=("Segoe UI", 15, "bold"),
                                 wraplength=980, justify="center")
        self.lbl_mode.pack(pady=4)
        self.lbl_link = tk.Label(body, bg=BG, font=("Segoe UI", 15, "bold"))
        self.lbl_link.pack(pady=(0, 2))
        self.lbl_detail = tk.Label(body, bg=BG, fg="#8a8f98",
                                   font=("Consolas", 10), wraplength=980)
        self.lbl_detail.pack(pady=(0, 6))

        # Controller diagram and touchpad sit side-by-side (not stacked) so
        # everything fits on a handheld's short-but-wide fullscreen display.
        views_row = tk.Frame(body, bg=BG)
        views_row.pack(pady=6)

        self.view = ControllerView(views_row)
        self.view.pack(side="left", padx=(0, 20))

        pad_col = tk.Frame(views_row, bg=BG)
        pad_col.pack(side="left", anchor="n")
        tk.Label(pad_col, bg=BG, fg="#9aa0a6", font=("Segoe UI", 11),
                 text="Touchpad (drag to move the receiver PC's mouse "
                      "cursor):").pack(anchor="w", pady=(0, 4))
        pad_row = tk.Frame(pad_col, bg=BG)
        pad_row.pack()
        self.touchpad = TouchPad(pad_row, self.engine)
        self.touchpad.pack(side="left", padx=(0, 14))
        self.btn_touch_click = tk.Button(pad_row, text="⬤\nClick",
                                         font=("Segoe UI", 12, "bold"),
                                         takefocus=0, bd=0, bg="#2b2f36",
                                         fg=FG, activebackground="#3a4048",
                                         activeforeground=FG)
        self.btn_touch_click.bind(
            "<ButtonPress-1>", lambda e: self.engine.set_touch_click(True))
        self.btn_touch_click.bind(
            "<ButtonRelease-1>", lambda e: self.engine.set_touch_click(False))
        self.btn_touch_click.pack(side="left", ipadx=18, ipady=28)

        tk.Label(self, bg=BG, fg="#777", font=("Segoe UI", 10),
                 text="Keep this window open while playing on the "
                      "receiver PC."
                 ).pack(side="bottom", pady=8)

        self._load_config()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(33, self._tick)

    # ---------------- actions
    def _on_mode_change(self):
        if self.mode.get() == "bt":
            self.row_cable.pack_forget()
            self.row_bt.pack(pady=4)
        else:
            self.row_bt.pack_forget()
            self.row_cable.pack(pady=4)

    def _scan_bt(self):
        self.btn_scan.config(state="disabled", text="Scanning...")

        def work():
            devs = list_paired_bluetooth()

            def done():
                self.btn_scan.config(state="normal",
                                     text="Scan paired devices")
                self.bt_devices = devs
                if devs:
                    vals = [f"{name}  [{mac}]" for name, mac in devs]
                    self.bt_combo["values"] = vals
                    self.bt_combo.set(vals[0])
                else:
                    messagebox.showwarning(
                        APP_NAME,
                        "No paired devices found.\n\nPair first:\n"
                        "Settings → Bluetooth → Add device → "
                        "choose the receiver PC → accept the PIN on both.",
                        parent=self)
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _selected_mac(self):
        m = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})",
                      self.bt_combo.get())
        return m.group(1).lower() if m else None

    def _toggle(self):
        active = (self.bt and self.bt.is_alive()) or \
                 (self.cable and self.cable.is_alive())
        if active:
            if self.bt:
                self.bt.stop()
                self.bt = None
            if self.cable:
                self.cable.stop()
                self.cable = None
            self.btn_start.config(text="▶   START STREAMING", bg="#2e7d32")
            return
        if self.mode.get() == "bt":
            mac = self._selected_mac()
            if not mac:
                messagebox.showerror(
                    APP_NAME,
                    "Touch 'Scan paired devices' and choose your receiver "
                    "PC first.", parent=self)
                return
            self.bt = BtStreamer(self.engine, mac)
            self.bt.start()
        else:
            ip = self.cable_ip.get().strip()
            if not ip:
                self.btn_start.config(text="Searching for receiver PC...",
                                      state="disabled")
                self.update_idletasks()
                ip = discover_over_cable()
                self.btn_start.config(state="normal")
                if not ip:
                    messagebox.showerror(
                        APP_NAME,
                        "Could not find the receiver PC over the USB "
                        "cable.\n\nCheck: is a USB DATA-LINK cable used "
                        "(not a plain charge cable)? Is the Receiver app "
                        "open on that PC? Is the cable fully plugged in "
                        "on both ends?",
                        parent=self)
                    self.btn_start.config(text="▶   START STREAMING",
                                          bg="#2e7d32")
                    return
                self.cable_ip.set(ip)
            self.cable = CableStreamer(self.engine, ip)
            self.cable.start()
        self._save_config()
        self.btn_start.config(text="■   STOP", bg="#c62828")

    # ---------------- periodic UI update
    def _tick(self):
        e = self.engine
        if e.controller_ok:
            self.lbl_ctrl.config(
                text=f"Controller detected  (slot {e.slot})  ✔", fg=GOOD)
            if e.activity:
                self.lbl_mode.config(
                    text="GAMEPAD MODE ✔  - your buttons are reaching "
                         "the app.", fg=GOOD)
            else:
                self.lbl_mode.config(
                    text="DESKTOP / MOUSE MODE DETECTED - that is why the "
                         "mouse moves when you use the sticks.\n"
                         "FIX:  press the Command Center button (or your "
                         "device's equivalent)  →  Control Mode  →  "
                         "GAMEPAD.\n"
                         "This text turns green the moment it works.",
                    fg=BAD)
        else:
            self.lbl_ctrl.config(text="Controller NOT detected", fg=BAD)
            self.lbl_mode.config(
                text="Press the Command Center button (or your device's "
                     "equivalent)  →  Control Mode  →  GAMEPAD,\nand close "
                     "any game running on this device.",
                fg=BAD)

        if self.bt and self.bt.is_alive():
            color = GOOD if self.bt.connected else WARN
            extra = f"   ({self.bt.pps} packets/s)" if self.bt.connected \
                else ""
            self.lbl_link.config(text=self.bt.status + extra, fg=color)
            self.lbl_detail.config(text=self.bt.detail)
            msg = "streaming over Bluetooth" if self.bt.connected \
                else "connecting..."
        elif self.cable and self.cable.is_alive():
            color = GOOD if self.cable.connected else WARN
            extra = f"   ({self.cable.pps} packets/s)" \
                if self.cable.connected else ""
            self.lbl_link.config(text=self.cable.status + extra, fg=color)
            self.lbl_detail.config(text="")
            msg = "streaming over USB cable" if self.cable.connected \
                else "connecting..."
        else:
            self.btn_start.config(text="▶   START STREAMING", bg="#2e7d32")
            if self.bt is not None:
                self.lbl_link.config(text=self.bt.status, fg=BAD)
                self.lbl_detail.config(text=self.bt.detail)
            elif self.cable is not None:
                self.lbl_link.config(text=self.cable.status, fg=BAD)
                self.lbl_detail.config(text="")
            else:
                self.lbl_link.config(text="Not streaming yet", fg="#9aa0a6")
                self.lbl_detail.config(text="")
            msg = "local view - press START when ready"
        self.view.show(e.latest, msg)
        self.after(33, self._tick)

    # ---------------- config / close
    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            if cfg.get("bt_choice"):
                self.bt_combo.set(cfg["bt_choice"])
            if cfg.get("cable_ip"):
                self.cable_ip.set(cfg["cable_ip"])
            self.mode.set(cfg.get("mode", "bt"))
        except Exception:
            pass
        self._on_mode_change()

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump({"bt_choice": self.bt_combo.get(),
                           "cable_ip": self.cable_ip.get().strip(),
                           "mode": self.mode.get()}, fh)
        except Exception:
            pass

    def _close(self):
        if self.bt:
            self.bt.stop()
        if self.cable:
            self.cable.stop()
        self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
