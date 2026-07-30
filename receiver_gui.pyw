"""WinPadBridge Receiver (GUI) -- run this ON THE PC you want to control games on.

Double-click this file to open it. No terminal needed.

It creates a virtual Xbox 360 controller (via the ViGEmBus driver) and
mirrors whatever the WinPadBridge Sender streams to it. Tabs:
  * Home            - status, this PC's IP, start/stop listener
  * Controller Test - live picture of what is arriving from the sender
  * Help            - setup steps and troubleshooting
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "WinPadBridge Receiver  (PC)"
DEFAULT_PORT = 47845
PACKET_FMT = "<IHBBhhhh"          # seq, buttons, LT, RT, LX, LY, RX, RY
PACKET_SIZE = struct.calcsize(PACKET_FMT)
DISCOVER_MSG = b"WINPADBRIDGE_DISCOVER"
HERE_MSG = b"WINPADBRIDGE_HERE"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".winpadbridge_receiver.json")

# ---------------------------------------------------------------- vgamepad --
VG_ERROR = ""
GAMEPAD = None
try:
    import vgamepad as vg
    GAMEPAD = vg.VX360Gamepad()
except Exception as e:                                   # driver/module missing
    VG_ERROR = str(e)


def pad_apply(state):
    """Push a state tuple into the virtual pad (if available)."""
    if GAMEPAD is None:
        return
    buttons, lt, rt, lx, ly, rx, ry = state
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


# ------------------------------------------------------------------ helpers --
def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def is_newer(seq, last):
    if last is None:
        return True
    return seq != last and ((seq - last) & 0xFFFFFFFF) < 0x80000000


# ---------------------------------------------------------------- engine ----
class Engine:
    """Background thread: listens on UDP, feeds the virtual pad."""

    def __init__(self, port):
        self.port = port
        self.stop_ev = threading.Event()
        self.latest = (0, 0, 0, 0, 0, 0, 0)
        self.link_active = False
        self.sender_ip = ""
        self.pps = 0
        self.error = ""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            self.error = f"Cannot listen on port {self.port}: {e}"
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
                    pad_neutral()                # never leave buttons stuck
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

            if data == DISCOVER_MSG:             # "Find receiver PC" feature
                try:
                    sock.sendto(HERE_MSG, addr)
                except OSError:
                    pass
                continue

            if len(data) != PACKET_SIZE:
                continue

            seq, *fields = struct.unpack(PACKET_FMT, data)
            if not is_newer(seq, last_seq):
                continue
            last_seq = seq
            count += 1
            self.latest = tuple(fields)
            self.sender_ip = addr[0]
            self.link_active = True
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
        # trigger bars
        c.create_rectangle(40, 20, 62, 100, outline="#777", width=2)
        self.i["LTf"] = c.create_rectangle(42, 98, 60, 98, fill="#4caf50", width=0)
        c.create_rectangle(478, 20, 500, 100, outline="#777", width=2)
        self.i["RTf"] = c.create_rectangle(480, 98, 498, 98, fill="#4caf50", width=0)
        c.create_text(51, 112, text="LT", fill="#999")
        c.create_text(489, 112, text="RT", fill="#999")
        # bumpers / start / back
        self._rect("LB", 85, 25, 160, 52, "LB")
        self._rect("RB", 380, 25, 455, 52, "RB")
        self._rect("BACK", 210, 30, 255, 52, "BACK")
        self._rect("START", 285, 30, 335, 52, "START")
        # sticks
        self.i["Lring"] = c.create_oval(90, 105, 180, 195, outline="#777", width=2)
        self.i["Ldot"] = c.create_oval(127, 142, 143, 158, fill="#4caf50", width=0)
        self.i["Rring"] = c.create_oval(360, 105, 450, 195, outline="#777", width=2)
        self.i["Rdot"] = c.create_oval(397, 142, 413, 158, fill="#4caf50", width=0)
        c.create_text(135, 205, text="L stick (click = L3)", fill="#999")
        c.create_text(405, 205, text="R stick (click = R3)", fill="#999")
        # d-pad
        self._rect("DPAD_UP", 215, 118, 240, 143, "▲")
        self._rect("DPAD_DOWN", 215, 173, 240, 198, "▼")
        self._rect("DPAD_LEFT", 190, 145, 215, 170, "◀")
        self._rect("DPAD_RIGHT", 240, 145, 265, 170, "▶")
        # ABXY
        for name, (cx, cy) in {"Y": (315, 122), "X": (289, 148),
                               "B": (341, 148), "A": (315, 174)}.items():
            self.i[name] = c.create_oval(cx - 13, cy - 13, cx + 13, cy + 13,
                                         outline="#777", width=2, fill="")
            c.create_text(cx, cy, text=name, fill="#ccc",
                          font=("Segoe UI", 9, "bold"))
        # numeric readout
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
        # stick clicks change the ring color
        self.itemconfig(self.i["Lring"],
                        outline="#e0a400" if buttons & BTN["L3"] else "#777")
        self.itemconfig(self.i["Rring"],
                        outline="#e0a400" if buttons & BTN["R3"] else "#777")
        # stick dots
        for dot, cx, cy, vx, vy in (("Ldot", 135, 150, lx, ly),
                                    ("Rdot", 405, 150, rx, ry)):
            x = cx + (vx / 32768.0) * 34
            y = cy - (vy / 32768.0) * 34
            self.coords(self.i[dot], x - 8, y - 8, x + 8, y + 8)
        # triggers
        self.coords(self.i["LTf"], 42, 98 - (lt / 255.0) * 76, 60, 98)
        self.coords(self.i["RTf"], 480, 98 - (rt / 255.0) * 76, 498, 98)
        self.itemconfig(self.i["txt"],
                        text=f"LX {lx:>6}  LY {ly:>6}   RX {rx:>6}  RY {ry:>6}")
        self.itemconfig(self.i["txt2"],
                        text=f"LT {lt:>3}  RT {rt:>3}   buttons 0x{buttons:04X}")
        self.itemconfig(self.i["msg"], text=msg)


# -------------------------------------------------------------------- app ---
HELP_TEXT = f"""HOW TO SET UP (this PC / receiver side)

1.  Install the ViGEmBus driver (one time only):
    open  github.com/nefarius/ViGEmBus/releases
    download  ViGEmBus_x64.exe  and run it.

2.  If a yellow warning shows on the Home tab, click
    "Install vgamepad", wait, then close and reopen this app.

3.  The first time you press Start, Windows Firewall may ask
    about Python. Click Allow and tick BOTH Private and Public
    networks (hotspot networks often count as Public).

    If you never saw that popup and nothing arrives, run this
    once in an admin terminal:
    netsh advfirewall firewall add rule name="WinPadBridge" dir=in action=allow protocol=UDP localport={DEFAULT_PORT}

4.  On the go: turn on this PC's Mobile Hotspot
    (Settings > Network & internet > Mobile hotspot) and
    connect the handheld to it. Your IP is usually 192.168.137.1.

READING THE STATUS
*  "Waiting for sender"      = listening, nothing arriving yet.
*  "Receiving from <ip>"     = packets are coming in. The
   Controller Test tab shows them live.
*  If packets arrive but games see nothing, the virtual pad is
   not active - fix step 1 and 2 above.

IN YOUR GAME OR EMULATOR
Any title that reads an Xbox 360 controller will pick this up
automatically. Some emulators need manual mapping, e.g. in PCSX2:
Settings > Controllers > Controller Port 1 > Automatic Mapping
and pick the Xbox 360 controller. Do this AFTER this app is
running (the pad must exist before the game/emulator scans)."""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.resizable(False, False)
        self.port = self._load_port()
        self.engine = Engine(self.port)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_home = ttk.Frame(nb)
        self.tab_test = ttk.Frame(nb)
        self.tab_help = ttk.Frame(nb)
        nb.add(self.tab_home, text="   Home   ")
        nb.add(self.tab_test, text="   Controller Test   ")
        nb.add(self.tab_help, text="   Help   ")

        self._build_home()
        self._build_test()
        self._build_help()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(33, self._tick)

    # ---------------- home tab
    def _build_home(self):
        f = self.tab_home
        pad = {"padx": 12, "pady": 6}

        self.lbl_pad = tk.Label(f, font=("Segoe UI", 11, "bold"))
        self.lbl_pad.grid(row=0, column=0, columnspan=3, sticky="w", **pad)

        if GAMEPAD is None:
            warn = tk.Frame(f, bg="#fff3cd", bd=1, relief="solid")
            warn.grid(row=1, column=0, columnspan=3, sticky="we", padx=12, pady=4)
            tk.Label(warn, bg="#fff3cd", justify="left", wraplength=470,
                     text=("vgamepad / ViGEmBus is not available, so games "
                           "cannot see a controller yet (monitor mode only).\n"
                           "1) Install the ViGEmBus driver (Help tab)   "
                           "2) Click the button below, then reopen this app.\n"
                           f"Details: {VG_ERROR[:120]}")
                     ).pack(side="left", padx=8, pady=6)
            tk.Button(warn, text="Install vgamepad",
                      command=self._install_vgamepad).pack(side="right",
                                                           padx=8, pady=6)

        tk.Label(f, text="This PC's IP (type this on the handheld):",
                 font=("Segoe UI", 10)).grid(row=2, column=0, sticky="w", **pad)
        self.lbl_ips = tk.Label(f, font=("Consolas", 12, "bold"), fg="#1a73e8")
        self.lbl_ips.grid(row=3, column=0, columnspan=2, sticky="w", padx=12)
        tk.Button(f, text="Refresh", command=self._refresh_ips)\
            .grid(row=3, column=2, sticky="e", padx=12)
        tk.Label(f, fg="#666",
                 text="Tip: with Mobile Hotspot it is usually 192.168.137.1")\
            .grid(row=4, column=0, columnspan=3, sticky="w", padx=12)

        tk.Label(f, text="Listen port:").grid(row=5, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar(value=str(self.port))
        tk.Entry(f, textvariable=self.port_var, width=8)\
            .grid(row=5, column=1, sticky="w")
        tk.Button(f, text="Restart listener", command=self._restart)\
            .grid(row=5, column=2, sticky="e", padx=12)

        self.lbl_link = tk.Label(f, font=("Segoe UI", 12, "bold"))
        self.lbl_link.grid(row=6, column=0, columnspan=3, sticky="w", **pad)
        self.lbl_err = tk.Label(f, fg="#c62828", wraplength=500, justify="left")
        self.lbl_err.grid(row=7, column=0, columnspan=3, sticky="w", padx=12)

        self._refresh_ips()

    # ---------------- test tab
    def _build_test(self):
        tk.Label(self.tab_test, justify="left",
                 text=("This shows what is ARRIVING from the sender, live.\n"
                       "If it moves here, the network part works.")
                 ).pack(anchor="w", padx=10, pady=6)
        self.view = ControllerView(self.tab_test)
        self.view.pack(padx=10, pady=4)

    # ---------------- help tab
    def _build_help(self):
        txt = tk.Text(self.tab_help, wrap="word", width=66, height=24,
                      font=("Consolas", 9))
        txt.insert("1.0", HELP_TEXT)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True, padx=8, pady=8)

    # ---------------- actions
    def _refresh_ips(self):
        ips = local_ips()
        self.lbl_ips.config(text="   ".join(ips) if ips else
                            "No network found - connect to Wi-Fi / hotspot")

    def _restart(self):
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "Port must be a number.")
            return
        self.engine.stop()
        self.port = port
        self._save_port()
        self.engine = Engine(port)

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

    # ---------------- periodic UI update
    def _tick(self):
        e = self.engine
        if GAMEPAD is not None:
            self.lbl_pad.config(text="Virtual Xbox 360 pad:  ACTIVE ✔",
                                fg="#2e7d32")
        else:
            self.lbl_pad.config(text="Virtual pad:  NOT ACTIVE (monitor mode)",
                                fg="#c62828")
        if e.link_active:
            self.lbl_link.config(fg="#2e7d32",
                                 text=f"Receiving from {e.sender_ip}"
                                      f"   -   {e.pps} packets/s")
            msg = f"live from {e.sender_ip}"
        else:
            self.lbl_link.config(fg="#e65100",
                                 text=f"Waiting for sender on port "
                                      f"{e.port}...")
            msg = "no data arriving"
        self.lbl_err.config(text=e.error)
        self.view.show(e.latest, msg)
        self.after(33, self._tick)

    # ---------------- config / close
    def _load_port(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return int(json.load(fh).get("port", DEFAULT_PORT))
        except Exception:
            return DEFAULT_PORT

    def _save_port(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump({"port": self.port}, fh)
        except OSError:
            pass

    def _close(self):
        self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
