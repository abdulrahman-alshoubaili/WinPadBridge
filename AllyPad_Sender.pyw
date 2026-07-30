"""AllyPad Sender (GUI) -- run this ON THE ROG ALLY X.

Double-click this file to open it. No terminal needed.
Needs only plain Python (3.11+). No extra packages.

Tabs:
  * Home            - find the laptop, start/stop streaming, status
  * Controller Test - live picture of what the Ally's controls output
  * Help            - setup steps and troubleshooting
"""

import ctypes
import json
import os
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "AllyPad Sender  (ROG Ally)"
DEFAULT_PORT = 47845
RATE_HZ = 250
PACKET_FMT = "<IHBBhhhh"          # seq, buttons, LT, RT, LX, LY, RX, RY
DISCOVER_MSG = b"ALLYPAD_DISCOVER"
HERE_MSG = b"ALLYPAD_HERE"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".allypad_sender.json")


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
    raise RuntimeError("XInput not found (this must run on Windows).")


# ---------------------------------------------------------------- discovery -
def discover_receiver(port, timeout=2.5):
    """Broadcast a hello; the receiver app answers with its address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    try:
        s.sendto(DISCOVER_MSG, ("255.255.255.255", port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(64)
            except socket.timeout:
                break
            if data == HERE_MSG:
                return addr[0]
    except OSError:
        pass
    finally:
        s.close()
    return None


# ------------------------------------------------------------------ engine --
class Engine:
    """Background thread: polls the Ally's gamepad, optionally streams it."""

    def __init__(self):
        self.stop_ev = threading.Event()
        self.sending = False
        self.target = None                     # (ip, port)
        self.controller_ok = False
        self.latest = (0, 0, 0, 0, 0, 0, 0)
        self.pps = 0
        self.error = ""
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    def run(self):
        try:
            xinput = load_xinput()
        except Exception as e:
            self.error = str(e)
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        state = XInputState()
        period = 1.0 / RATE_HZ
        seq = 0
        count = 0
        t0 = time.monotonic()

        while not self.stop_ev.is_set():
            ok = xinput.XInputGetState(0, ctypes.byref(state)) == 0
            self.controller_ok = ok
            if ok:
                g = state.Gamepad
                self.latest = (g.wButtons, g.bLeftTrigger, g.bRightTrigger,
                               g.sThumbLX, g.sThumbLY,
                               g.sThumbRX, g.sThumbRY)
                if self.sending and self.target:
                    seq = (seq + 1) & 0xFFFFFFFF
                    try:
                        sock.sendto(struct.pack(PACKET_FMT, seq, *self.latest),
                                    self.target)
                        count += 1
                    except OSError as e:
                        self.error = f"Send failed: {e}"
                        self.sending = False
                time.sleep(period)
            else:
                self.latest = (0, 0, 0, 0, 0, 0, 0)
                time.sleep(0.3)

            now = time.monotonic()
            if now - t0 >= 1.0:
                self.pps = count
                count = 0
                t0 = now


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
        c.create_text(135, 205, text="L stick (click = L3)", fill="#999")
        c.create_text(405, 205, text="R stick (click = R3)", fill="#999")
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
HELP_TEXT = """HOW TO USE (Ally side)

1.  Open the AllyPad Receiver app on the laptop FIRST.

2.  Connect the Ally to the SAME network as the laptop.
    Best on the go: the laptop's Mobile Hotspot.

3.  Press "Find my laptop". If it finds it, the IP fills in
    automatically. If not, type the IP shown on the
    receiver's Home tab (hotspot is usually 192.168.137.1).

4.  Press "Start streaming".

CHECKING THINGS
*  Controller Test tab must react when you press buttons.
   If it does NOT: the Ally's controls are in Desktop /
   Mouse mode. Open Armoury Crate / Command Center and set
   the control mode to Gamepad, then try again.
*  Close any game running on the Ally itself - a game can
   grab the controller exclusively.
*  "Find my laptop" fails on networks that block broadcast
   (some public Wi-Fi). Use the laptop hotspot, or type the
   IP by hand.

Streaming keeps running while this window is open."""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.resizable(False, False)
        self.engine = Engine()

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
        self._load_config()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(33, self._tick)

    # ---------------- home tab
    def _build_home(self):
        f = self.tab_home
        pad = {"padx": 12, "pady": 8}

        tk.Label(f, text="Laptop IP:", font=("Segoe UI", 10))\
            .grid(row=0, column=0, sticky="w", **pad)
        self.ip_var = tk.StringVar()
        tk.Entry(f, textvariable=self.ip_var, width=18,
                 font=("Consolas", 12)).grid(row=0, column=1, sticky="w")
        self.btn_find = tk.Button(f, text="Find my laptop",
                                  command=self._find)
        self.btn_find.grid(row=0, column=2, padx=12)

        tk.Label(f, text="Port:").grid(row=1, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        tk.Entry(f, textvariable=self.port_var, width=8)\
            .grid(row=1, column=1, sticky="w")

        self.btn_start = tk.Button(f, text="▶  Start streaming",
                                   font=("Segoe UI", 13, "bold"),
                                   bg="#2e7d32", fg="white", width=22,
                                   command=self._toggle)
        self.btn_start.grid(row=2, column=0, columnspan=3, pady=14)

        self.lbl_ctrl = tk.Label(f, font=("Segoe UI", 11, "bold"))
        self.lbl_ctrl.grid(row=3, column=0, columnspan=3, sticky="w", padx=12)
        self.lbl_link = tk.Label(f, font=("Segoe UI", 11))
        self.lbl_link.grid(row=4, column=0, columnspan=3, sticky="w", padx=12)
        self.lbl_err = tk.Label(f, fg="#c62828", wraplength=480,
                                justify="left")
        self.lbl_err.grid(row=5, column=0, columnspan=3, sticky="w",
                          padx=12, pady=(0, 10))

    # ---------------- test tab
    def _build_test(self):
        tk.Label(self.tab_test, justify="left",
                 text=("This shows what the Ally's controls are outputting, "
                       "live - even before you press Start.\n"
                       "If nothing reacts here, switch the Ally's control "
                       "mode to Gamepad (Armoury Crate / Command Center).")
                 ).pack(anchor="w", padx=10, pady=6)
        self.view = ControllerView(self.tab_test)
        self.view.pack(padx=10, pady=4)

    # ---------------- help tab
    def _build_help(self):
        txt = tk.Text(self.tab_help, wrap="word", width=64, height=22,
                      font=("Consolas", 9))
        txt.insert("1.0", HELP_TEXT)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True, padx=8, pady=8)

    # ---------------- actions
    def _find(self):
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "Port must be a number.")
            return
        self.btn_find.config(state="disabled", text="Searching...")

        def work():
            ip = discover_receiver(port)
            def done():
                self.btn_find.config(state="normal", text="Find my laptop")
                if ip:
                    self.ip_var.set(ip)
                else:
                    messagebox.showwarning(
                        APP_NAME,
                        "Laptop not found.\n\nCheck that:\n"
                        "1. The Receiver app is OPEN on the laptop\n"
                        "2. Both devices are on the SAME network\n"
                        "3. The laptop firewall allowed Python\n\n"
                        "You can also type the IP shown on the "
                        "receiver's Home tab.")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _toggle(self):
        e = self.engine
        if not e.sending:
            ip = self.ip_var.get().strip()
            if not ip:
                messagebox.showerror(APP_NAME,
                                     "Enter the laptop IP first, or press "
                                     "'Find my laptop'.")
                return
            try:
                port = int(self.port_var.get())
            except ValueError:
                messagebox.showerror(APP_NAME, "Port must be a number.")
                return
            e.error = ""
            e.target = (ip, port)
            e.sending = True
            self._save_config()
            self.btn_start.config(text="■  Stop streaming", bg="#c62828")
        else:
            e.sending = False
            self.btn_start.config(text="▶  Start streaming", bg="#2e7d32")

    # ---------------- periodic UI update
    def _tick(self):
        e = self.engine
        if e.controller_ok:
            self.lbl_ctrl.config(text="Controller:  DETECTED ✔", fg="#2e7d32")
        else:
            self.lbl_ctrl.config(
                text="Controller:  NOT DETECTED  "
                     "(set Gamepad mode in Armoury Crate)",
                fg="#c62828")
        if e.sending and e.target:
            self.lbl_link.config(
                text=f"Streaming to {e.target[0]}:{e.target[1]}   -   "
                     f"{e.pps} packets/s", fg="#2e7d32")
            msg = "streaming"
        else:
            self.lbl_link.config(text="Not streaming", fg="#666")
            msg = "not streaming (local view only)"
        self.lbl_err.config(text=e.error)
        self.view.show(e.latest, msg)
        self.after(33, self._tick)

    # ---------------- config / close
    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            self.ip_var.set(cfg.get("ip", ""))
            self.port_var.set(str(cfg.get("port", DEFAULT_PORT)))
        except Exception:
            pass

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump({"ip": self.ip_var.get().strip(),
                           "port": int(self.port_var.get())}, fh)
        except Exception:
            pass

    def _close(self):
        self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
