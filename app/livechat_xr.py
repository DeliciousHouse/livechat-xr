"""LiveChat XR tray app: settings window + chat runner. The OpenXR layer does the drawing in-game."""
import ctypes
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import winreg
from logging.handlers import RotatingFileHandler
from tkinter import ttk, messagebox

import pystray
from PIL import Image, ImageDraw

import chat

APP = "LiveChat XR"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
FIELDS = [  # (section, key, label)
    ("chat", "channel", "Channel / username"),
    ("games", "exes", "Games (exe names, comma-separated)"),
    ("banner", "seconds", "Seconds on screen"),
    ("banner", "max_lines", "Comments per banner"),
    ("banner", "up", "Height above eye line (m)"),
    ("banner", "distance", "Distance (m)"),
    ("banner", "width", "Width (m)"),
    ("chat", "tiktok_sign_api_key", "TikTok sign API key (optional)"),
    ("chat", "discord_webhook", "Discord webhook URL (optional)"),
]


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "LiveChatXR")
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            frozen = getattr(sys, "frozen", False)
            cmd = f'"{sys.executable}"' if frozen else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(k, "LiveChatXR", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "LiveChatXR")
            except OSError:
                pass


def icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 8, 60, 44), 10, fill=(124, 77, 255))
    d.polygon([(16, 42), (30, 42), (14, 58)], fill=(124, 77, 255))
    for x in (20, 32, 44):
        d.ellipse((x - 4, 22, x + 4, 30), fill="white")
    return img


class App:
    def __init__(self):
        self.runner = chat.Runner()
        self.cfg = chat.load_config()
        self.ui: queue.Queue = queue.Queue()  # tray thread -> Tk thread
        self.root = tk.Tk()
        self.root.withdraw()
        self.win: tk.Toplevel | None = None
        self.icon = pystray.Icon("LiveChatXR", icon_image(), APP, pystray.Menu(
            pystray.MenuItem("Settings…", lambda: self.ui.put(self.open_settings), default=True),
            pystray.MenuItem("Send test banner", lambda: self.ui.put(self.test_banner)),
            pystray.MenuItem("Open data folder", lambda: os.startfile(chat.DIR)),
            pystray.MenuItem("Quit", lambda: self.ui.put(self.quit)),
        ))

    def run(self):
        chat.DIR.mkdir(parents=True, exist_ok=True)
        if not (chat.DIR / "config.ini").exists():
            chat.save_config(self.cfg)  # the layer reads [games]/[banner] from here
        self.icon.run_detached()
        self.runner.start(self.cfg)
        if not self.cfg["chat"]["channel"].strip():
            self.open_settings()
        self.root.after(200, self.pump)
        self.root.mainloop()

    def pump(self):
        while not self.ui.empty():
            self.ui.get()()
        self.icon.title = f"{APP}: {self.runner.status}"[:127]
        if self.win and self.win.winfo_exists():
            self.status_var.set(self.runner.status)
        self.root.after(500, self.pump)

    def test_banner(self):
        text = "LiveChat XR: test banner ✔\nIf you can read this in your headset, you're set."
        chat.write_banner(text)
        webhook = self.cfg["chat"].get("discord_webhook", "").strip()
        if webhook:
            threading.Thread(target=chat.post_discord, args=(webhook, text), daemon=True).start()

    def open_settings(self):
        if self.win and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            return
        w = self.win = tk.Toplevel(self.root)
        w.title(f"{APP} settings")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=14)
        f.grid()
        ttk.Label(f, text="Platform").grid(row=0, column=0, sticky="w", pady=3)
        self.platform = tk.StringVar(value=self.cfg["chat"]["platform"].lower())
        pf = ttk.Frame(f)
        pf.grid(row=0, column=1, sticky="w")
        for val, text in (("twitch", "Twitch"), ("tiktok", "TikTok LIVE")):
            ttk.Radiobutton(pf, text=text, value=val, variable=self.platform).pack(side="left", padx=(0, 10))
        self.vars = {}
        for i, (sec, key, label) in enumerate(FIELDS, start=1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=self.cfg[sec][key])
            ttk.Entry(f, textvariable=v, width=36, show="•" if "key" in key or "webhook" in key else "").grid(row=i, column=1, pady=3)
            self.vars[(sec, key)] = v
        n = len(FIELDS) + 1
        self.auto = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(f, text="Start with Windows", variable=self.auto).grid(row=n, column=1, sticky="w", pady=6)
        ttk.Label(f, text="Placement and game changes apply the next time the game starts.",
                  foreground="#666").grid(row=n + 1, column=0, columnspan=2, sticky="w")
        self.status_var = tk.StringVar(value=self.runner.status)
        ttk.Label(f, textvariable=self.status_var, foreground="#4527a0").grid(row=n + 2, column=0, columnspan=2, sticky="w", pady=6)
        bf = ttk.Frame(f)
        bf.grid(row=n + 3, column=0, columnspan=2, sticky="e")
        ttk.Button(bf, text="Send test banner", command=self.test_banner).pack(side="left", padx=4)
        ttk.Button(bf, text="Save", command=self.save).pack(side="left")

    def save(self):
        for (sec, key), v in self.vars.items():
            val = v.get().strip()
            if sec == "banner":
                try:
                    if (int(val) if key == "max_lines" else float(val)) <= 0 and key != "up":
                        raise ValueError
                except ValueError:
                    messagebox.showerror(APP, f"'{val}' is not a valid number for {key}.", parent=self.win)
                    return
            self.cfg[sec][key] = val
        self.cfg["chat"]["platform"] = self.platform.get()
        chat.save_config(self.cfg)
        set_autostart(self.auto.get())
        self.runner.start(self.cfg)
        self.win.withdraw()

    def quit(self):
        self.runner.stop()
        self.icon.stop()
        self.root.destroy()


def main():
    if sys.argv[1:] == ["--check"]:  # packaging smoke test (CI): exit 0 if the bundled deps import
        import TikTokLive.events  # noqa: F401
        sys.exit(0)
    chat.DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[RotatingFileHandler(chat.DIR / "app.log", maxBytes=1 << 20, backupCount=1, encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ctypes.windll.kernel32.CreateMutexW(None, False, "LiveChatXR.single-instance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, f"{APP} is already running (see the tray).", APP, 0x40)
        return
    App().run()


if __name__ == "__main__":
    main()
