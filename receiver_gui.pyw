"""WinPadBridge Receiver v0.5 -- run this ON THE PC you want to control games on.

Bluetooth only (plus optional USB cable). Double-click to open. No terminal,
no firewall, no IPs.

Creates a virtual Xbox 360 controller (via the ViGEmBus driver) and
mirrors whatever the WinPadBridge Sender streams over Bluetooth.
"""

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
PACKET_FMT = "<IHBBhhhh"            # seq, buttons, LT, RT, LX, LY, RX, RY
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

# ---------------------------------------------------------------- vgamepad --
VG_ERROR = ""
GAMEPAD = None
try:
    import vgamepad as vg
    GAMEPAD = vg.VX360Gamepad()
except Exception as e:
    VG_ERROR = str(e)

_pad_lock = threading.Lock()


def pad_apply(state):
    if GAMEPAD is None:
        return
    buttons, lt, rt, lx, ly, rx, ry = state
    with _pad_lock:
        r = GAMEPAD.report
        r.wButtons = buttons
        r.bLeftTrigger = lt
        r.bRightTrigger = rt
        r.sThumbLX = lx
        r.sThumbLY = ly
        r.sThumbRX = rx
        r.sThumbRY = ry
        GAMEPAD.update()


def pad_neutral():
    pad_apply((0, 0, 0, 0, 0, 0, 0))


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
        self.latest = (0, 0, 0, 0, 0, 0, 0)
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
                        pad_neutral()               # stalled link safety
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
                        pad_apply(self.latest)
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
            self.latest = (0, 0, 0, 0, 0, 0, 0)
            pad_neutral()
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
        self.latest = (0, 0, 0, 0, 0, 0, 0)
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
                    self.latest = (0, 0, 0, 0, 0, 0, 0)
                    pad_neutral()
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
            pad_apply(self.latest)

        sock.close()
        pad_neutral()


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
of Bluetooth and press START."""


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

        if GAMEPAD is None:
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

        tk.Label(f, fg="#666", justify="left",
                 text="While this window is open, games see a normal "
                      "Xbox 360 controller.\nExample (PCSX2): Settings > "
                      "Controllers > Port 1 > Automatic Mapping.")\
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

    def _tick(self):
        if GAMEPAD is not None:
            self.lbl_pad.config(text="Virtual Xbox 360 pad:  ACTIVE ✔",
                                fg="#2e7d32")
        else:
            self.lbl_pad.config(text="Virtual pad:  NOT ACTIVE "
                                     "(monitor mode)", fg="#c62828")
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
            state, msg = bt.latest, "live over Bluetooth"
        elif cab.link_active:
            state, msg = cab.latest, "live over USB cable"
        else:
            state, msg = (0, 0, 0, 0, 0, 0, 0), "no data arriving"
        self.view.show(state, msg)
        self.after(33, self._tick)

    def _close(self):
        self.bt.stop()
        self.cable.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
