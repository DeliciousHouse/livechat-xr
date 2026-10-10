"""LiveChat XR tray app: settings window + chat runner. The OpenXR layer does the drawing in-game."""
import argparse
import ctypes
import hashlib
import logging
import os
import queue
import sys
import threading
import tempfile
from pathlib import Path
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
    ("chat", "tiktok", "TikTok handle (optional)"),
    ("chat", "twitch", "Twitch channel (optional)"),
    ("chat", "youtube", "YouTube handle (optional)"),
    ("games", "exes", "Games (exe names, comma-separated)"),
    ("banner", "seconds", "Seconds on screen"),
    ("banner", "max_lines", "Comments per banner"),
    ("banner", "up", "Height above eye line (m)"),
    ("banner", "distance", "Distance (m)"),
    ("banner", "width", "Width (m)"),
    ("chat", "follow_ask", "TikTok follow ask (empty = off)"),
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
    def __init__(self, settings=False, portable=False):
        self.settings, self.portable = settings, portable
        self.runner = chat.Runner()
        self.cfg = chat.load_config(portable=portable)
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
        if self.settings or not chat.channels(self.cfg):
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
        w.title(f"{APP} settings" + (" — alternate data profile" if self.portable else ""))
        w.resizable(False, False)
        f = ttk.Frame(w, padding=14)
        f.grid()
        ttk.Label(f, text="Fill in any of TikTok, Twitch and YouTube: chat from all of them shares the banner.",
                  foreground="#666").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.vars = {}
        for i, (sec, key, label) in enumerate(FIELDS, start=1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=self.cfg[sec][key])
            ttk.Entry(f, textvariable=v, width=36, show="•" if "key" in key or "webhook" in key else "").grid(row=i, column=1, pady=3)
            self.vars[(sec, key)] = v
        n = len(FIELDS) + 1
        self.show_tag = tk.BooleanVar(value=self.cfg.getboolean("chat", "show_tag", fallback=True))
        ttk.Checkbutton(f, text="Show LiveChat XR tag", variable=self.show_tag).grid(row=n, column=1, sticky="w", pady=6)
        n += 1
        self.auto = tk.BooleanVar(value=False if self.portable else autostart_enabled())
        auto = ttk.Checkbutton(f, text="Start with Windows", variable=self.auto)
        auto.grid(row=n, column=1, sticky="w", pady=6)
        if self.portable:
            auto.state(["disabled"])
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
        self.cfg["chat"]["show_tag"] = str(bool(self.show_tag.get())).lower()
        chat.save_config(self.cfg)
        if not self.portable:
            set_autostart(self.auto.get())
        self.runner.start(self.cfg)
        self.win.withdraw()

    def quit(self):
        self.runner.stop()
        self.icon.stop()
        self.root.destroy()


def mutex_name(directory=None):
    if directory is None:
        return "LiveChatXR.single-instance"
    digest = hashlib.sha256(os.path.normcase(str(directory.resolve())).encode()).hexdigest()
    return "LiveChatXR.profile." + digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP)
    parser.add_argument("--check", action="store_true", help="check bundled dependencies and exit")
    parser.add_argument("--settings", action="store_true", help="open Settings even with a configured channel")
    parser.add_argument("--data-dir", type=Path, help="absolute alternate profile; no autostart or layer registration")
    args = parser.parse_args(argv)
    if args.check:  # packaging smoke test (CI): exit 0 if the bundled deps import
        import TikTokLive.events  # noqa: F401
        return
    if args.data_dir is not None:
        try:
            if not args.data_dir.is_absolute():
                raise ValueError("--data-dir must be absolute")
            directory = args.data_dir.resolve()
            owner = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "LiveChatXR").resolve()
            if directory == owner or owner in directory.parents or directory in owner.parents:
                raise ValueError("--data-dir must be separate from the installed profile")
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory):
                pass  # fail before runner/UI, with no fallback to the installed profile
        except (OSError, ValueError) as e:
            parser.error(str(e))
        chat.DIR = directory
    chat.DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[RotatingFileHandler(chat.DIR / "app.log", maxBytes=1 << 20, backupCount=1, encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, mutex_name(chat.DIR if args.data_dir is not None else None))
    if not handle:
        raise ctypes.WinError()
    try:
        if kernel.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(None, f"{APP} profile is already running (see the tray).", APP, 0x40)
            return
        App(settings=args.settings, portable=args.data_dir is not None).run()
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
