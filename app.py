import sys

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--worker":
    import mount_worker
    sys.exit(mount_worker.run_worker(sys.argv[2:]))

import ctypes
import importlib.util
import os
import queue
import string
import subprocess
import tempfile
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

from fatx import PartitionInfo, human_size, scan_physical_drives

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable if FROZEN else __file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))
WORKER = HERE / "mount_worker.py"
IS_WINDOWS = sys.platform == "win32"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    args = sys.argv[1:] if FROZEN else [str(Path(__file__).resolve())] + sys.argv[1:]
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                        subprocess.list2cmdline(args), None, 1)
    sys.exit(0)


def winfsp_installed() -> bool:
    if not IS_WINDOWS:
        return True
    import winreg
    for key in (r"SOFTWARE\WOW6432Node\WinFsp", r"SOFTWARE\WinFsp"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                path, _ = winreg.QueryValueEx(k, "InstallDir")
                if os.path.isdir(path):
                    return True
        except OSError:
            continue
    return False


def bundled_winfsp_msi() -> Path | None:
    for folder in (BUNDLE, HERE):
        msi = folder / "winfsp.msi"
        if msi.is_file():
            return msi
    return None


def install_winfsp(msi: Path) -> tuple[bool, str]:
    """Silently install WinFsp from the bundled MSI. Needs admin. Returns (ok, message)."""
    log = Path(tempfile.gettempdir()) / "XboxMounter-winfsp-install.log"
    try:
        r = subprocess.run(["msiexec", "/i", str(msi), "/qn", "/norestart", "/l*v", str(log)],
                           timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if r.returncode in (0, 3010, 1641) and winfsp_installed():
        return True, "restart recommended" if r.returncode != 0 else ""
    return False, f"Installer exit code {r.returncode}. Log: {log}"


def fuse_lib_available() -> bool:
    if FROZEN:
        return True
    return any(importlib.util.find_spec(m) for m in ("mfusepy", "fuse"))


def free_drive_letters() -> list[str]:
    if not IS_WINDOWS:
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [L for i, L in enumerate(string.ascii_uppercase)
            if L not in "AB" and not (mask >> i) & 1]


def asset(name: str) -> Path | None:
    """Find a bundled file (inside the exe, or next to app.py when run from source)."""
    for folder in (BUNDLE, HERE):
        p = folder / name
        if p.is_file():
            return p
    return None


def worker_command() -> list[str]:
    if FROZEN:
        return [sys.executable, "--worker"]
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        exe = exe.parent / "python.exe"
    return [str(exe), "-u", str(WORKER)]


BG = "#141414"
SURFACE = "#1c1c1c"
SURFACE2 = "#262626"
FIELD = "#202020"
BORDER = "#333333"
HOVER = "#303030"
PRESSED = "#3a3a3a"
FG = "#ececec"
MUTED = "#9a9a9a"
DISABLED = "#5c5c5c"
ACCENT = "#1F6AA5"
ACCENT_HOVER = "#2A7DBF"
WARN = "#e3b341"
ERROR = "#f85149"
BANNER_BG, BANNER_BORDER, BANNER_FG = "#2a2310", "#5c4a14", "#f0d58a"
DIALOG_BG = "#222222"
FONT = ("Segoe UI", 10)


def apply_dark_theme(root: tk.Tk) -> None:
    from tkinter import font as tkfont
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG, troughcolor=SURFACE,
                    focuscolor=ACCENT, selectbackground=ACCENT, selectforeground="#ffffff",
                    insertcolor=FG, font=FONT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
    style.configure("Status.TLabel", background=SURFACE, foreground=MUTED, padding=(10, 5))

    style.configure("TButton", background=SURFACE2, foreground=FG, bordercolor=BORDER,
                    lightcolor=SURFACE2, darkcolor=SURFACE2, padding=(12, 5), relief="flat",
                    width=-6)
    style.map("TButton",
              background=[("disabled", SURFACE), ("pressed", PRESSED), ("active", HOVER)],
              foreground=[("disabled", DISABLED)],
              lightcolor=[("pressed", PRESSED), ("active", HOVER)],
              darkcolor=[("pressed", PRESSED), ("active", HOVER)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT)
    style.map("Accent.TButton",
              background=[("disabled", SURFACE), ("pressed", ACCENT), ("active", ACCENT_HOVER)],
              foreground=[("disabled", DISABLED)],
              bordercolor=[("disabled", BORDER)],
              lightcolor=[("disabled", SURFACE), ("active", ACCENT_HOVER)],
              darkcolor=[("disabled", SURFACE), ("active", ACCENT_HOVER)])

    style.configure("TCheckbutton", background=BG, foreground=FG,
                    indicatorbackground=FIELD, indicatorforeground="#ffffff",
                    upperbordercolor=BORDER, lowerbordercolor=BORDER)
    style.map("TCheckbutton", background=[("active", BG)],
              indicatorbackground=[("selected", ACCENT), ("active", HOVER)])

    style.configure("TCombobox", fieldbackground=FIELD, background=SURFACE2, foreground=FG,
                    arrowcolor=FG, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD, padding=4)
    style.map("TCombobox",
              fieldbackground=[("disabled", SURFACE), ("readonly", FIELD)],
              foreground=[("disabled", DISABLED), ("readonly", FG)],
              arrowcolor=[("disabled", DISABLED)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
              background=[("active", HOVER)])
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", FONT)

    row_h = tkfont.Font(root=root, font=FONT).metrics("linespace") + 12
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=FG,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE, rowheight=row_h)
    style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=SURFACE2, foreground=MUTED, relief="flat",
                    bordercolor=BORDER, lightcolor=SURFACE2, darkcolor=SURFACE2, padding=(8, 5))
    style.map("Treeview.Heading", background=[("active", HOVER)])

    style.configure("Vertical.TScrollbar", background=SURFACE2, troughcolor=SURFACE,
                    bordercolor=SURFACE, lightcolor=SURFACE2, darkcolor=SURFACE2, arrowcolor=MUTED)
    style.map("Vertical.TScrollbar", background=[("active", HOVER)])


def set_dark_titlebar(win) -> None:
    """Ask Windows 10/11 to draw this window's title bar dark."""
    if not IS_WINDOWS:
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.c_void_p(int(win.wm_frame(), 16))
        on = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on),
                                                          ctypes.sizeof(on)) == 0:
                break
        SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)
    except Exception:
        pass


class DarkDialog(tk.Toplevel):
    """Dark replacement for tkinter.messagebox (which always follows the light system style)."""
    GLYPHS = {"warning": ("⚠", WARN), "error": ("✕", ERROR), "question": ("?", ACCENT), "info": ("i", ACCENT)}

    def __init__(self, parent, title, message, kind, buttons, default, cancel):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(bg=DIALOG_BG)
        self.resizable(False, False)
        self.transient(parent)
        self.result = cancel

        body = tk.Frame(self, bg=DIALOG_BG)
        body.pack(fill="both", expand=True, padx=22, pady=(20, 16))
        glyph, color = self.GLYPHS.get(kind, self.GLYPHS["info"])
        tk.Label(body, text=glyph, fg=color, bg=DIALOG_BG, font=("Segoe UI", 22, "bold")).pack(
            side="left", anchor="n", padx=(0, 16))
        tk.Label(body, text=message, fg=FG, bg=DIALOG_BG, font=FONT, justify="left",
                 wraplength=440).pack(side="left", fill="both", expand=True)

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x")
        for label, value in reversed(buttons):
            b = ttk.Button(row, text=label, command=lambda v=value: self._done(v),
                           style="Accent.TButton" if value == default else "TButton")
            b.pack(side="right", padx=(0, 12), pady=12)
            if value == default:
                b.focus_set()
        self.bind("<Return>", lambda e: self._done(default))
        self.bind("<Escape>", lambda e: self._done(cancel))
        self.protocol("WM_DELETE_WINDOW", lambda: self._done(cancel))

        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        x = px + max((parent.winfo_width() - self.winfo_reqwidth()) // 2, 0)
        y = py + max((parent.winfo_height() - self.winfo_reqheight()) // 3, 0)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        set_dark_titlebar(self)
        self.grab_set()
        self.wait_window()

    def _done(self, value):
        self.result = value
        self.destroy()


def ask(parent, title, message, kind="question", yes="Yes", no="No", default_yes=False) -> bool:
    d = DarkDialog(parent, title, message, kind, [(yes, True), (no, False)],
                   default=default_yes, cancel=False)
    return bool(d.result)


def notify(parent, title, message, kind="error") -> None:
    DarkDialog(parent, title, message, kind, [("OK", True)], default=True, cancel=True)


class Mount:
    def __init__(self, part: PartitionInfo, letter: str, proc: subprocess.Popen, readonly: bool):
        self.part = part
        self.letter = letter
        self.proc = proc
        self.readonly = readonly
        self.mounted = False
        self.unmounting = False
        self.log: list[str] = []


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("Xbox 360 Drive Mounter")
        self.geometry("840x480")
        self.minsize(720, 400)
        apply_dark_theme(self)
        self._set_icon()

        self.admin = is_admin()
        self.disks: dict[str, list[PartitionInfo]] = {}
        self.chosen: dict[str, int] = {}
        self.mounts: dict[str, Mount] = {}
        self.events: queue.Queue = queue.Queue()
        self.warned_rw = False
        self.setting_up = False

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(0, self._reveal)
        self.after(150, self.pump_events)
        self.after(50, self.ensure_winfsp)
        self.after(100, self.refresh)

    def _reveal(self):
        self.deiconify()
        set_dark_titlebar(self)

    def _build(self):
        top = ttk.Frame(self, padding=(12, 12, 12, 0))
        top.pack(fill="x")

        if not winfsp_installed() and not (bundled_winfsp_msi() and self.admin):
            self._banner(top, "WinFsp is not installed - it's needed to create drive letters.",
                         "Get WinFsp", lambda: webbrowser.open("https://winfsp.dev/rel/"))
        if not fuse_lib_available():
            self._banner(top, "Python package missing. Run:  pip install mfusepy", None, None)
        if IS_WINDOWS and not self.admin:
            self._banner(top, "Administrator rights are needed to read physical disks.",
                         "Restart as admin", relaunch_as_admin)

        ttk.Label(top, text="Xbox 360 drives", style="Title.TLabel").pack(anchor="w")

        mid = ttk.Frame(self, padding=12)
        mid.pack(fill="both", expand=True)
        cols = ("disk", "partition", "size", "drive")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse", height=5)
        for col, text, width in (("disk", "Disk", 330), ("partition", "Partition", 150),
                                 ("size", "Size", 90), ("drive", "Mounted as", 110)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_buttons())
        self.tree.bind("<Double-1>", lambda e: self.open_selected())

        actions = ttk.Frame(self, padding=(12, 0, 12, 12))
        actions.pack(fill="x", side="bottom", before=mid)
        opts = ttk.Frame(self, padding=(12, 0, 12, 10))
        opts.pack(fill="x", side="bottom", before=mid)

        ttk.Label(opts, text="Partition:").pack(side="left")
        self.part_var = tk.StringVar()
        self.part_box = ttk.Combobox(opts, textvariable=self.part_var, width=30, state="disabled")
        self.part_box.pack(side="left", padx=(6, 18))
        self.part_box.bind("<<ComboboxSelected>>", self.on_partition_chosen)
        ttk.Label(opts, text="Drive letter:").pack(side="left")
        self.letter_var = tk.StringVar()
        self.letter_box = ttk.Combobox(opts, textvariable=self.letter_var, width=5, state="readonly")
        self.letter_box.pack(side="left", padx=(6, 18))
        self.letter_box.bind("<<ComboboxSelected>>", lambda e: self.letter_box.selection_clear())
        self.ro_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Read-only", variable=self.ro_var).pack(side="left")

        ttk.Button(actions, text="Refresh", command=self.refresh).pack(side="left")
        self.btn_unmount = ttk.Button(actions, text="Unmount", command=self.unmount_selected)
        self.btn_unmount.pack(side="right")
        self.btn_open = ttk.Button(actions, text="Open", command=self.open_selected)
        self.btn_open.pack(side="right", padx=6)
        self.btn_mount = ttk.Button(actions, text="Mount", command=self.mount_selected,
                                    style="Accent.TButton")
        self.btn_mount.pack(side="right")

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, style="Status.TLabel",
                  anchor="w").pack(fill="x", side="bottom", before=actions)
        self.update_letters()
        self.update_buttons()

    def _banner(self, parent, text, btn_text, cmd):
        f = tk.Frame(parent, bg=BANNER_BG, highlightbackground=BANNER_BORDER, highlightthickness=1)
        f.pack(fill="x", pady=(0, 10))
        tk.Label(f, text="⚠  " + text, bg=BANNER_BG, fg=BANNER_FG, font=FONT,
                 anchor="w").pack(side="left", padx=10, pady=7)
        if btn_text:
            ttk.Button(f, text=btn_text, command=cmd).pack(side="right", padx=6, pady=5)
        return f

    def ensure_winfsp(self):
        if winfsp_installed():
            return
        msi = bundled_winfsp_msi()
        if not msi or not self.admin:
            return
        self.setting_up = True
        self.status.set("One-time setup: installing drive support (WinFsp)…")
        self.update_buttons()

        def work():
            ok, msg = install_winfsp(msi)
            self.events.put(("winfsp", ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def selected_disk(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] in self.disks else None

    def chosen_part(self, disk: str) -> PartitionInfo | None:
        m = self.mounts.get(disk)
        if m:
            return m.part
        parts = self.disks.get(disk) or []
        off = self.chosen.get(disk)
        return next((p for p in parts if p.offset == off), parts[0] if parts else None)

    def selected(self) -> PartitionInfo | None:
        disk = self.selected_disk()
        return self.chosen_part(disk) if disk else None

    @staticmethod
    def part_label(p: PartitionInfo, largest: bool) -> str:
        return f"{p.name}  -  {human_size(p.size)}" + ("  (largest)" if largest else "")

    def update_partition_box(self):
        disk = self.selected_disk()
        parts = self.disks.get(disk) or [] if disk else []
        if not parts:
            self.part_box["values"] = []
            self.part_var.set("")
            self.part_box.configure(state="disabled")
            return
        labels = [self.part_label(p, i == 0) for i, p in enumerate(parts)]
        self.part_box["values"] = labels
        cur = self.chosen_part(disk)
        idx = next((i for i, p in enumerate(parts) if p.offset == cur.offset), 0)
        self.part_var.set(labels[idx])
        self.part_box.configure(state="disabled" if disk in self.mounts else "readonly")

    def on_partition_chosen(self, _event=None):
        disk = self.selected_disk()
        idx = self.part_box.current()
        self.part_box.selection_clear()
        if disk and idx >= 0 and disk not in self.mounts:
            self.chosen[disk] = self.disks[disk][idx].offset
            self.render()

    def update_letters(self):
        used = {m.letter for m in self.mounts.values()}
        letters = [L for L in free_drive_letters() if L not in used]
        self.letter_box["values"] = [f"{L}:" for L in letters]
        if self.letter_var.get().rstrip(":") not in letters:
            self.letter_var.set(f"{letters[-1]}:" if letters else "")

    def update_buttons(self):
        part = self.selected()
        m = self.mounts.get(part.source) if part else None
        self.update_partition_box()
        self.btn_mount.state(["!disabled"] if part and not m and not self.setting_up else ["disabled"])
        self.btn_unmount.state(["!disabled"] if m and not m.unmounting else ["disabled"])
        self.btn_open.state(["!disabled"] if m and m.mounted and not m.unmounting else ["disabled"])

    def render(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for disk in self.disks:
            p = self.chosen_part(disk)
            m = self.mounts.get(disk)
            if not m:
                drive = ""
            elif m.unmounting:
                drive = "unmounting…"
            elif m.mounted:
                drive = f"{m.letter}:  ({'read-only' if m.readonly else 'read-write'})"
            else:
                drive = "mounting…"
            self.tree.insert("", "end", iid=disk, values=(p.disk, p.name, human_size(p.size), drive))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        elif self.disks and not sel:
            self.tree.selection_set(next(iter(self.disks)))
        self.update_letters()
        self.update_buttons()

    def _set_icon(self):
        ico, png = asset("logo.ico"), asset("logo.png")
        try:
            if png:
                self._icon_img = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_img)
            if ico and IS_WINDOWS:
                self.iconbitmap(default=str(ico))
        except tk.TclError:
            pass
        if ico and IS_WINDOWS:
            self.after(50, lambda: self._set_taskbar_icon(ico))

    def _set_taskbar_icon(self, ico: Path):
        try:
            from ctypes import c_int, c_uint, c_void_p, c_wchar_p
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = c_void_p
            user32.LoadImageW.argtypes = [c_void_p, c_wchar_p, c_uint, c_int, c_int, c_uint]
            user32.SendMessageW.restype = c_void_p
            user32.SendMessageW.argtypes = [c_void_p, c_uint, c_void_p, c_void_p]
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
            ICON_SMALL, ICON_BIG = 0, 1
            SM_CXSMICON, SM_CXICON = 49, 11
            self.update_idletasks()
            hwnd = int(self.wm_frame(), 16)
            self._hicons = []
            for kind, metric, minimum in ((ICON_SMALL, SM_CXSMICON, 16), (ICON_BIG, SM_CXICON, 32)):
                size = max(user32.GetSystemMetrics(metric), minimum)
                h = user32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
                if h:
                    user32.SendMessageW(hwnd, WM_SETICON, kind, h)
                    self._hicons.append(h)
        except Exception:
            pass

    def refresh(self):
        self.status.set("Scanning disks…")
        self.config(cursor="watch")

        def work():
            parts, denied = scan_physical_drives()
            self.events.put(("scanned", parts, denied))

        threading.Thread(target=work, daemon=True).start()


    def mount_selected(self):
        part = self.selected()
        letter = self.letter_var.get().rstrip(":")
        if not part or not letter:
            return
        if not winfsp_installed():
            notify(self, "WinFsp required", "Install WinFsp from https://winfsp.dev first.")
            return
        readonly = self.ro_var.get()
        if not readonly and not self.warned_rw:
            ok = ask(
                self, "Mount read-write",
                "You're mounting with write access, so changes go straight to the Xbox drive.\n\n"
                "• Back up anything important first.\n"
                "• Always click Unmount (or close this app) before unplugging the drive.\n"
                "• Names are limited to 42 characters, letters/numbers and !#$%&'()-.@[]^_`{}~\n"
                "• Files can't be bigger than 4 GB.\n\nContinue?",
                "warning", yes="Continue", no="Cancel", default_yes=True)
            if not ok:
                return
            self.warned_rw = True
        mountpoint = f"\\\\.\\{letter}:" if self.admin else f"{letter}:"
        cmd = worker_command() + [
               "--source", part.source,
               "--offset", str(part.offset), "--size", str(part.size),
               "--mount", mountpoint, "--label", f"Xbox 360 {part.name}", "--stdin-control"]
        if readonly:
            cmd.append("--readonly")
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", creationflags=flags, cwd=str(HERE), env=env)
        except OSError as exc:
            notify(self, "Mount failed", str(exc))
            return
        m = Mount(part, letter, proc, readonly)
        self.mounts[part.source] = m
        threading.Thread(target=self._watch, args=(part.source, m), daemon=True).start()
        self.status.set(f"Mounting {part.name} as {letter}:…")
        self.render()

    def _watch(self, key: str, m: Mount):
        for line in m.proc.stdout:
            line = line.rstrip()
            m.log.append(line)
            if line == "MOUNTED":
                self.events.put(("mounted", key))
        m.proc.wait()
        self.events.put(("exited", key))

    @staticmethod
    def _stop(m: Mount, timeout: float = 60):
        """Ask the worker to flush and exit; force-kill only as a last resort."""
        if m.proc.poll() is not None:
            return
        try:
            m.proc.stdin.write("UNMOUNT\n")
            m.proc.stdin.flush()
            m.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            m.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            m.proc.kill()

    def unmount(self, key: str):
        m = self.mounts.get(key)
        if not m or m.unmounting:
            return
        m.unmounting = True
        self.status.set(f"Unmounting {m.letter}: - finishing writes…")
        self.render()
        threading.Thread(target=self._stop, args=(m,), daemon=True).start()

    def unmount_selected(self):
        part = self.selected()
        if part:
            self.unmount(part.source)

    def open_selected(self):
        part = self.selected()
        m = self.mounts.get(part.source) if part else None
        if m and m.mounted:
            os.startfile(f"{m.letter}:\\")

    def pump_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "scanned":
                    _, parts, denied = ev
                    disks: dict[str, list[PartitionInfo]] = {}
                    for p in parts:
                        disks.setdefault(p.source, []).append(p)
                    for disk, m in self.mounts.items():
                        disks.setdefault(disk, [m.part])
                    for lst in disks.values():
                        lst.sort(key=lambda p: p.size, reverse=True)
                    self.disks = disks
                    self.chosen = {d: off for d, off in self.chosen.items()
                                   if d in disks and any(p.offset == off for p in disks[d])}
                    self.config(cursor="")
                    msg = f"Found {len(disks)} Xbox 360 drive(s)."
                    if denied:
                        msg += "  (Some disks couldn't be read - run as administrator.)"
                    if self.setting_up:
                        msg += "  Setting up drive support…"
                    self.status.set(msg)
                    self.render()
                elif kind == "winfsp":
                    _, ok, info = ev
                    self.setting_up = False
                    if ok:
                        self.status.set("Drive support installed - ready." +
                                        (f" ({info})" if info else ""))
                    else:
                        self.status.set("Drive support setup failed.")
                        notify(
                            self, "Setup failed",
                            "Couldn't install the drive driver (WinFsp), so drives can't be "
                            f"mounted yet.\n\n{info}\n\nYou can install it manually from "
                            "https://winfsp.dev/rel/ and restart this app.")
                    self.update_buttons()
                elif kind == "mounted":
                    m = self.mounts.get(ev[1])
                    if m:
                        m.mounted = True
                        mode = "read-only" if m.readonly else "read-write"
                        self.status.set(f"{m.part.name} mounted as {m.letter}: ({mode}).")
                        self.render()
                elif kind == "exited":
                    m = self.mounts.pop(ev[1], None)
                    if m:
                        if m.mounted:
                            self.status.set(f"{m.letter}: unmounted.")
                        else:
                            detail = "\n".join(m.log[-12:]) or "No output from mount process."
                            self.status.set("Mount failed.")
                            notify(self, "Mount failed", detail)
                        self.render()
        except queue.Empty:
            pass
        self.after(150, self.pump_events)

    def on_close(self):
        if self.mounts:
            n = len(self.mounts)
            question = (f"{n} drive{'s are' if n > 1 else ' is'} still mounted.\n\n"
                        "Closing will safely unmount "
                        f"{'them' if n > 1 else 'it'}. Make sure any copying has finished first.\n\n"
                        "Are you sure you want to close?")
        else:
            question = "Are you sure you want to close Xbox 360 Drive Mounter?"
        if not ask(self, "Close app?", question, "warning", yes="Close", no="Cancel"):
            return
        if self.mounts:
            self.status.set("Unmounting and saving changes…")
            self.config(cursor="watch")
            self.update()
            threads = [threading.Thread(target=self._stop, args=(m,)) for m in self.mounts.values()]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.destroy()


if __name__ == "__main__":
    if IS_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("XboxMounter.App")
        except Exception:
            pass
    App().mainloop()