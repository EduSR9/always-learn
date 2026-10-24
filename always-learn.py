"""Always Learn: Anki pops up while the AI is thinking (Windows, macOS beta)."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Optional
from xml.sax.saxutils import escape

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    from pystray import Icon, Menu, MenuItem
except ModuleNotFoundError as exc:
    if exc.name not in {"PIL", "pystray"}:
        raise
    raise SystemExit(
        "Always Learn needs its UI dependencies. From this repository folder, "
        "run: python -m pip install -r requirements.txt"
    ) from exc

IS_MAC = sys.platform == "darwin"

try:
    import comtypes
    import comtypes.client
except ModuleNotFoundError:
    # Without UI Automation we cannot read browser URLs and fall back to titles.
    comtypes = None

if sys.platform == "win32":
    import winreg


APP_NAME = "Always Learn"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
ASSETS_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "assets"
ICON_PATH = ASSETS_DIR / "icon.png"
ICO_PATH = ASSETS_DIR / "icon.ico"
SOUND_PATH = ASSETS_DIR / "open.wav"
CONFIG_PATH = Path.home() / ".always-learn.json"
STARTUP_FLAG = "--startup"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_TASK_NAME = APP_NAME
# Where the startup copy of the app lives (outside Downloads/OneDrive).
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP_NAME
INSTALLED_EXE = INSTALL_DIR / "always-learn.exe"
SHOW_EVENT_NAME = "Local\\always-learn-show-window"
ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_STARTUP_TIMEOUT_SECONDS = 60
PROMPT_SEND_DELAY_SECONDS = 0.4
# How long to wait for the AI's Stop button to appear after a prompt is sent.
GENERATION_START_TIMEOUT_SECONDS = 20
# The Stop button must be gone this long before the answer counts as finished.
# Agentic/coding AIs briefly hide it between steps while they "think out loud".
GENERATION_END_GRACE_SECONDS = 3.0
# A click only counts as sending a prompt if the AI starts answering this soon.
CLICK_SEND_WINDOW_SECONDS = 3.0

COLORS = {
    "background": "#0E1117",
    "surface": "#161B24",
    "surface_high": "#1F2633",
    "border": "#2A3342",
    "text": "#F2F4F8",
    "muted": "#8B97A8",
    "blue": "#4EA8FF",
    "blue_hover": "#7ABEFF",
    "on_blue": "#0A1424",
    "yellow": "#FFC94A",
    "green": "#4ADE80",
    "red": "#F87171",
}


def ui_font(size: int, bold: bool = False) -> tuple:
    if IS_MAC:  # Tk on macOS uses 72 dpi points, so sizes need a bump.
        return ("Helvetica Neue", round(size * 1.3), "bold") if bold else ("Helvetica Neue", round(size * 1.3))
    return ("Segoe UI Semibold", size) if bold else ("Segoe UI", size)


FONT = ui_font(10)
FONT_BOLD = ui_font(10, bold=True)
FONT_SMALL = ui_font(9)
FONT_SECTION = ui_font(8, bold=True)
FONT_STATUS = ui_font(11, bold=True)
FONT_TITLE = ui_font(15, bold=True)
START_AT_LOGIN_LABEL = "Start at login" if IS_MAC else "Start with Windows"

DEFAULT_AI_SITES = [
    "chatgpt.com",
    "chat.openai.com",
    "claude.ai",
    "gemini.google.com",
    "aistudio.google.com",
    "copilot.microsoft.com",
    "perplexity.ai",
    "chat.deepseek.com",
    "grok.com",
    "chat.mistral.ai",
    "poe.com",
]
# Windows executables and macOS app names (lowercase).
DEFAULT_AI_APPS = ["claude.exe", "chatgpt.exe", "perplexity.exe", "claude", "chatgpt", "perplexity"]
# Apps with a built-in AI chat where Enter is also used for typing: code
# editors (Claude Code, Copilot, Cursor...) and Obsidian (Copilot plugin). Here a
# prompt only counts once the AI's Stop button actually appears.
DEFAULT_IDE_APPS = [
    "code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe", "trae.exe", "kiro.exe",
    "code", "visual studio code", "code - insiders", "cursor", "windsurf", "trae", "kiro",
    "obsidian.exe", "obsidian",
]
IDE_NAMES = {
    "code.exe": "VS Code", "code - insiders.exe": "VS Code Insiders", "cursor.exe": "Cursor",
    "windsurf.exe": "Windsurf", "trae.exe": "Trae", "kiro.exe": "Kiro",
    "code": "VS Code", "visual studio code": "VS Code", "code - insiders": "VS Code Insiders",
    "cursor": "Cursor", "windsurf": "Windsurf", "trae": "Trae", "kiro": "Kiro",
    "obsidian.exe": "Obsidian", "obsidian": "Obsidian",
}
# Used only when a browser's address bar cannot be read.
AI_TITLE_HINTS = [
    "chatgpt", "claude", "gemini", "copilot", "perplexity", "deepseek", "grok",
]
BROWSERS = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "opera.exe": "Opera",
    "vivaldi.exe": "Vivaldi",
    "arc.exe": "Arc",
    "zen.exe": "Zen",
}
# AI chats show a button like "Stop streaming" / "Detener respuesta" while
# they are generating an answer.
STOP_BUTTON_PREFIXES = (
    "stop", "detener", "parar", "interromp", "arrêter", "arreter", "stopp", "停止", "中止",
)
STOP_BUTTON_EXCLUDED_WORDS = (
    "dict", "voice", "voz", "record", "grab", "audio", "listen", "escuch",
    "aloud", "share", "compart",
    # IDE controls that are not the AI: the debugger's "Stop (Shift+F5)" etc.
    "f5", "debug", "task", "terminal",
)


@dataclass
class Config:
    enabled: bool = True
    # Minimize Anki and go back to the chat once the AI finishes answering.
    return_when_done: bool = True
    # Play a short chime when Anki pops up.
    sound_on_open: bool = True
    mode: str = "configured"
    configured_deck: str = ""
    random_decks: list[str] = field(default_factory=list)
    # Named sets of decks for random mode, e.g. {"Chinese": ["Mandarin Phrases", ...]}.
    deck_presets: dict[str, list[str]] = field(default_factory=dict)
    active_preset: str = ""
    cooldown_seconds: int = 0
    ai_sites: list[str] = field(default_factory=lambda: DEFAULT_AI_SITES.copy())
    ai_apps: list[str] = field(default_factory=lambda: DEFAULT_AI_APPS.copy())
    ide_apps: list[str] = field(default_factory=lambda: DEFAULT_IDE_APPS.copy())
    anki_path: str = ""


def merge_defaults(saved: list, defaults: list[str]) -> list[str]:
    items = [str(item).lower() for item in saved]
    return items + [item for item in defaults if item not in items]


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Always Learn: ignoring unreadable {CONFIG_PATH}: {exc}", file=sys.stderr)
        return Config()
    known = {item.name for item in fields(Config)}
    config = Config(**{key: value for key, value in data.items() if key in known})
    config.random_decks = [str(deck) for deck in config.random_decks]
    if not isinstance(config.deck_presets, dict):
        config.deck_presets = {}
    config.deck_presets = {str(name): [str(deck) for deck in decks] for name, decks in config.deck_presets.items()}
    # Saved lists keep the user's own additions; newly added defaults are merged in.
    config.ai_sites = merge_defaults(config.ai_sites, DEFAULT_AI_SITES)
    config.ai_apps = merge_defaults(config.ai_apps, DEFAULT_AI_APPS)
    config.ide_apps = merge_defaults(config.ide_apps, DEFAULT_IDE_APPS)
    config.cooldown_seconds = max(0, int(config.cooldown_seconds))
    return config


def save_config(config: Config) -> None:
    CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


def choose_deck(config: Config, previous: Optional[str] = None) -> str:
    if config.mode == "random":
        if not config.random_decks:
            raise ValueError("Tick at least one deck for random mode.")
        # Never repeat the previous deck, so the decks visibly rotate.
        others = [deck for deck in config.random_decks if deck != previous]
        return random.choice(others or config.random_decks)
    if not config.configured_deck:
        raise ValueError("Choose an Anki deck first.")
    return config.configured_deck


# --------------------------------------------------------------------------
# Win32 helpers
# --------------------------------------------------------------------------

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
    ]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_QUIT = 0x0012
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_S = 0x53
KEYEVENTF_KEYUP = 0x0002
LLKHF_INJECTED = 0x10
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_GETOBJECT = 0x003D
OBJID_CLIENT = -4
UIA_ROOT_OBJECT_ID = -25
SMTO_ABORTIFHUNG = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_exe_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def find_anki_window() -> Optional[int]:
    """Return Anki's main window ("User 1 - Anki"), ignoring editors etc."""
    found: list[int] = []

    @WNDENUMPROC
    def visit(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or not window_class(hwnd).startswith("Qt"):
            return True
        title = window_title(hwnd)
        if title == "Anki" or title.endswith(" - Anki"):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def bring_window_to_front(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    foreground = user32.GetForegroundWindow()
    if foreground == hwnd:
        return
    current_thread = kernel32.GetCurrentThreadId()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
    if user32.GetForegroundWindow() != hwnd:
        # Windows only lets the process that sent the last input change focus.
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)


def acquire_single_instance() -> bool:
    if sys.platform != "win32":
        return True
    kernel32.CreateMutexW(None, False, "Local\\always-learn-single-instance")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def ask_running_instance_to_show() -> None:
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)


def listen_for_show_requests(on_request: Callable[[], None]) -> None:
    """Call on_request whenever a second launch asks this instance to show."""
    event = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)

    def wait() -> None:
        while True:
            if kernel32.WaitForSingleObject(event, 0xFFFFFFFF) == WAIT_OBJECT_0:
                on_request()

    threading.Thread(target=wait, daemon=True).start()


def foreground_window() -> Optional[int]:
    return user32.GetForegroundWindow()


def window_exists(handle: int) -> bool:
    return bool(user32.IsWindow(handle))


def minimize_window(handle: int) -> None:
    user32.ShowWindow(handle, SW_MINIMIZE)


def missing_permissions() -> list[str]:
    """Windows needs no special permissions."""
    return []


class InputHooks:
    """Reports Enter presses and left clicks (never what is typed)."""

    def __init__(
        self, on_enter: Callable[[int], None], on_click: Callable[[int], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.on_enter, self.on_click, self.on_error = on_enter, on_click, on_error
        self._thread_id = 0

    def start(self) -> None:
        threading.Thread(target=self._listen, daemon=True).start()

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _listen(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        enter_down = False

        @HOOKPROC
        def on_key(code: int, message: int, data: int) -> int:
            nonlocal enter_down
            if code == 0:
                key = ctypes.cast(data, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if key.vkCode == VK_RETURN and not key.flags & LLKHF_INJECTED:
                    if message == WM_KEYDOWN and not enter_down:
                        enter_down = True
                        # Shift+Enter inserts a newline instead of sending.
                        if not user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
                            self.on_enter(user32.GetForegroundWindow())
                    elif message == WM_KEYUP:
                        enter_down = False
            return user32.CallNextHookEx(None, code, message, data)

        @HOOKPROC
        def on_mouse(code: int, message: int, data: int) -> int:
            if code == 0 and message == WM_LBUTTONDOWN:
                self.on_click(user32.GetForegroundWindow())
            return user32.CallNextHookEx(None, code, message, data)

        module = kernel32.GetModuleHandleW(None)
        keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, on_key, module, 0)
        mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, on_mouse, module, 0)
        if not keyboard_hook:
            self.on_error("Could not watch the Enter key; prompt detection is off.")
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        for hook in (keyboard_hook, mouse_hook):
            if hook:
                user32.UnhookWindowsHookEx(hook)


# --------------------------------------------------------------------------
# Windows integration: start with Windows, desktop shortcut
# --------------------------------------------------------------------------


def launch_target() -> tuple[str, list[str]]:
    """The program and arguments that start Always Learn without a console."""
    if FROZEN:
        return sys.executable, []
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else sys.executable), [str(Path(__file__).resolve())]


def _run_hidden(command: list[str]) -> subprocess.CompletedProcess:
    """Run a console tool without flashing a console window."""
    return subprocess.run(
        command, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def install_for_startup() -> Path:
    """Copy the exe to a normal app folder and return that copy.

    Downloads and OneDrive folders can be slow to open right after sign-in, so
    the startup task runs this copy instead. It is refreshed whenever a newer
    download is opened.
    """
    current = Path(sys.executable)
    if current.resolve() == INSTALLED_EXE.resolve():
        return INSTALLED_EXE
    try:
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        if not INSTALLED_EXE.exists() or (
            (INSTALLED_EXE.stat().st_size, INSTALLED_EXE.stat().st_mtime)
            != (current.stat().st_size, current.stat().st_mtime)
        ):
            shutil.copy2(current, INSTALLED_EXE)
        return INSTALLED_EXE
    except OSError as exc:  # e.g. the installed copy is running right now
        print(f"Always Learn: could not copy to {INSTALL_DIR}: {exc}", file=sys.stderr)
        return INSTALLED_EXE if INSTALLED_EXE.exists() else current


def _startup_task_xml(program: str, arguments: str) -> str:
    user = escape(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts Always Learn when you sign in.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals>
    <Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(program)}</Command><Arguments>{escape(arguments)}</Arguments></Exec>
  </Actions>
</Task>"""


def _remove_legacy_run_entry() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except OSError:
        pass


def _has_legacy_run_entry() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def starts_with_windows() -> bool:
    task = _run_hidden(["schtasks", "/Query", "/TN", STARTUP_TASK_NAME])
    return task.returncode == 0 or _has_legacy_run_entry()


def set_start_with_windows(enabled: bool) -> None:
    """Start at sign-in through Task Scheduler, which runs right after sign-in
    instead of waiting in Windows' delayed startup-apps queue."""
    if not enabled:
        _run_hidden(["schtasks", "/Delete", "/TN", STARTUP_TASK_NAME, "/F"])
        _remove_legacy_run_entry()
        return
    if FROZEN:
        program, arguments = str(install_for_startup()), [STARTUP_FLAG]
    else:
        program, script = launch_target()
        arguments = [*script, STARTUP_FLAG]
    task_file = Path(tempfile.gettempdir()) / "always-learn-task.xml"
    task_file.write_text(_startup_task_xml(program, subprocess.list2cmdline(arguments)), encoding="utf-16")
    try:
        created = _run_hidden(["schtasks", "/Create", "/TN", STARTUP_TASK_NAME, "/XML", str(task_file), "/F"])
    finally:
        task_file.unlink(missing_ok=True)
    if created.returncode == 0:
        _remove_legacy_run_entry()
        return
    # Fall back to the classic startup entry if Task Scheduler is unavailable.
    print(f"Always Learn: Task Scheduler failed: {created.stderr.strip()}", file=sys.stderr)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline([program, *arguments]))


def refresh_start_at_login() -> None:
    """Keep an enabled startup entry current: move old startup-list entries to
    Task Scheduler and point it at the newest copy of the app."""
    if starts_with_windows():
        set_start_with_windows(True)


def create_desktop_shortcut() -> Optional[Path]:
    if comtypes is None:
        return None
    shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
    path = Path(shell.SpecialFolders("Desktop")) / f"{APP_NAME}.lnk"
    program, arguments = launch_target()
    shortcut = shell.CreateShortcut(str(path))
    shortcut.TargetPath = program
    shortcut.Arguments = subprocess.list2cmdline(arguments)
    shortcut.WorkingDirectory = str(APP_DIR)
    shortcut.IconLocation = program if FROZEN else str(ICO_PATH)
    shortcut.Description = "Review Anki while the AI is thinking"
    shortcut.Save()
    return path


# --------------------------------------------------------------------------
# Anki
# --------------------------------------------------------------------------


class AnkiUnavailable(RuntimeError):
    pass


def anki_connect(action: str, timeout: float = 3, **params: object) -> object:
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        ANKI_CONNECT_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise AnkiUnavailable(str(exc)) from exc
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result.get("result")


def list_anki_decks() -> list[str]:
    result = anki_connect("deckNames")
    if not isinstance(result, list):
        raise RuntimeError("AnkiConnect returned an unexpected deck list.")
    return sorted(str(deck) for deck in result)


def current_review_deck() -> Optional[str]:
    """Name of the deck Anki is reviewing right now, or None."""
    try:
        card = anki_connect("guiCurrentCard")
    except RuntimeError:  # "Gui review is not currently active."
        return None
    return str(card.get("deckName")) if isinstance(card, dict) else None


def find_anki_executable(config: Config) -> Optional[Path]:
    candidates = [Path(config.anki_path)] if config.anki_path else []
    for base in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(base)
        if root:
            candidates.append(Path(root) / ("Programs/Anki" if base == "LOCALAPPDATA" else "Anki") / "anki.exe")
    return next((path for path in candidates if path.is_file()), None)


def launch_anki(config: Config, background: bool = False) -> None:
    executable = find_anki_executable(config)
    if executable is None:
        raise RuntimeError(
            "Could not find anki.exe. Start Anki manually once, or set "
            f'"anki_path" in {CONFIG_PATH}.'
        )
    subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_anki_connect(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            anki_connect("version", timeout=1)
            return True
        except AnkiUnavailable:
            time.sleep(0.5)
    return False


_anki_launched_at = 0.0
_anki_launch_lock = threading.Lock()


def anki_process_running() -> bool:
    """Anki can be running (still loading) before its window appears."""
    result = _run_hidden(["tasklist", "/FI", "IMAGENAME eq anki.exe", "/NH"])
    return "anki.exe" in result.stdout.lower()


def ensure_anki_running(config: Config, background: bool = False) -> bool:
    """Start Anki unless it is already running or starting. Returns True if it
    was launched now. Two copies would fight over the collection ("Anki already
    open"), so this is the only place that launches Anki."""
    global _anki_launched_at
    with _anki_launch_lock:
        recently_launched = time.monotonic() - _anki_launched_at < ANKI_STARTUP_TIMEOUT_SECONDS
        if recently_launched or find_anki_window() or anki_process_running():
            return False
        launch_anki(config, background=background)
        _anki_launched_at = time.monotonic()
        return True


def start_anki_in_background(config: Config) -> None:
    """Launch Anki at login and minimize it, so later reviews open instantly."""
    try:
        if not ensure_anki_running(config, background=True):
            return
    except RuntimeError as exc:
        print(f"Always Learn: {exc}", file=sys.stderr)
        return
    deadline = time.monotonic() + ANKI_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        anki_window = find_anki_window()
        if anki_window:
            minimize_window(anki_window)
            return
        time.sleep(0.5)


def press_study_now(anki_window: int) -> None:
    """Press Anki's "Study Now" shortcut (S) on the deck overview."""
    if user32.GetForegroundWindow() != anki_window:
        raise RuntimeError("Windows did not let Anki come to the front, so Study Now was not pressed.")
    user32.keybd_event(VK_S, 0, 0, 0)
    user32.keybd_event(VK_S, 0, KEYEVENTF_KEYUP, 0)


def play_open_sound() -> None:
    """A short chime as the card pops up (non-blocking)."""
    if not SOUND_PATH.exists():
        return
    try:
        if IS_MAC:
            subprocess.Popen(["afplay", str(SOUND_PATH)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            import winsound

            winsound.PlaySound(str(SOUND_PATH), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (OSError, RuntimeError) as exc:
        print(f"Always Learn: could not play the sound: {exc}", file=sys.stderr)


def open_anki_deck(deck_name: str, config: Config) -> None:
    """Open Anki (launching it if needed), focus it and show a card from deck_name."""
    try:
        anki_connect("version", timeout=1)
    except AnkiUnavailable:
        # Not answering yet: start Anki if needed, or wait while it loads.
        ensure_anki_running(config)
        if not wait_for_anki_connect(ANKI_STARTUP_TIMEOUT_SECONDS):
            anki_window = find_anki_window()
            if anki_window:
                bring_window_to_front(anki_window)
                raise RuntimeError(
                    "Anki is open but AnkiConnect is not responding. "
                    "Install the AnkiConnect add-on and restart Anki."
                )
            raise RuntimeError(
                "Anki did not become ready in time. Is the AnkiConnect add-on installed?"
            )

    if deck_name not in list_anki_decks():
        raise RuntimeError(f"Deck not found in Anki: {deck_name}")
    anki_window = find_anki_window()
    if anki_window is None:
        raise RuntimeError("Could not find the Anki window.")

    reviewing = current_review_deck()
    if reviewing == deck_name or (reviewing or "").startswith(deck_name + "::"):
        bring_window_to_front(anki_window)  # keep the card that is already up
        if config.sound_on_open:
            play_open_sound()
        return
    if reviewing:
        anki_connect("guiDeckBrowser")
        time.sleep(0.4)

    # AnkiConnect's guiDeckReview leaves the overview on screen in recent Anki
    # versions, so open the overview and press Study Now like a person would.
    # Right after startup Anki may still be loading its collection.
    for attempt in range(10):
        try:
            if anki_connect("guiDeckOverview", timeout=5, name=deck_name):
                break
        except RuntimeError:
            if attempt == 9:
                raise
        time.sleep(0.5)
    else:
        raise RuntimeError(f"Anki could not open {deck_name}.")

    bring_window_to_front(anki_window)
    if config.sound_on_open:
        play_open_sound()
    time.sleep(0.5)  # let the overview page finish rendering
    for _ in range(2):
        press_study_now(anki_window)
        time.sleep(0.6)
        if current_review_deck():
            return
    raise RuntimeError(f"No card appeared. Does {deck_name} have cards due today?")


# --------------------------------------------------------------------------
# AI window detection
# --------------------------------------------------------------------------


def host_of(address: str) -> Optional[str]:
    value = address.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    host = value.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    if not host or " " in host or "." not in host:
        return None  # the user is typing a search, not a URL
    return host


def matches_site(host: str, sites: list[str]) -> Optional[str]:
    return next((site for site in sites if host == site or host.endswith("." + site)), None)


def is_stop_button(name: str) -> bool:
    name = name.strip().lower()
    return name.startswith(STOP_BUTTON_PREFIXES) and not any(
        word in name for word in STOP_BUTTON_EXCLUDED_WORDS
    )


class AIWindowDetector:
    """Identifies AI chats by browser URL (via UI Automation) or app name."""

    def __init__(self) -> None:
        self._uia = None
        self._uia_module = None
        self._address_bars: dict[int, object] = {}
        self._urls: dict[int, tuple[str, str, float]] = {}
        self._exe_names: dict[int, str] = {}
        self._accessibility_enabled: set[int] = set()
        if comtypes is None:
            return
        try:
            comtypes.CoInitialize()
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia

            self._uia_module = uia
            self._uia = comtypes.CoCreateInstance(
                uia.CUIAutomation._reg_clsid_,
                interface=uia.IUIAutomation,
                clsctx=comtypes.CLSCTX_INPROC_SERVER,
            )
            self._document_condition = self._uia.CreatePropertyCondition(
                uia.UIA_ControlTypePropertyId, uia.UIA_DocumentControlTypeId
            )
            self._button_condition = self._uia.CreatePropertyCondition(
                uia.UIA_ControlTypePropertyId, uia.UIA_ButtonControlTypeId
            )
            self._name_cache = self._uia.CreateCacheRequest()
            self._name_cache.AddProperty(uia.UIA_NamePropertyId)
        except (OSError, ImportError, AttributeError) as exc:
            self._uia = None
            print(f"Always Learn: UI Automation unavailable: {exc}", file=sys.stderr)

    def exe_name(self, hwnd: int) -> str:
        pid = window_pid(hwnd)
        if pid not in self._exe_names:
            self._exe_names[pid] = process_exe_name(pid)
        return self._exe_names[pid]

    def _read_address_bar(self, hwnd: int) -> str:
        uia = self._uia_module
        element = self._address_bars.get(hwnd)
        if element is None:
            root = self._uia.ElementFromHandle(hwnd)
            condition = self._uia.CreatePropertyCondition(
                uia.UIA_ControlTypePropertyId, uia.UIA_EditControlTypeId
            )
            element = root.FindFirst(uia.TreeScope_Descendants, condition)
            if not element:
                return ""
            self._address_bars[hwnd] = element
        try:
            return str(element.GetCurrentPropertyValue(uia.UIA_ValueValuePropertyId) or "")
        except (OSError, comtypes.COMError):
            self._address_bars.pop(hwnd, None)
            return ""

    def browser_url(self, hwnd: int, title: str) -> str:
        if self._uia is None:
            return ""
        cached = self._urls.get(hwnd)
        if cached and cached[0] == title and time.monotonic() - cached[2] < 2:
            return cached[1]
        try:
            url = self._read_address_bar(hwnd)
        except (OSError, comtypes.COMError):
            url = ""
        self._urls[hwnd] = (title, url, time.monotonic())
        return url

    def _enable_accessibility(self, hwnd: int) -> None:
        """Ask Chromium/Electron windows to expose their page, as a screen reader would.

        VS Code, Cursor and other Electron apps keep their UI hidden from UI
        Automation until someone asks for it with WM_GETOBJECT.
        """
        if hwnd in self._accessibility_enabled:
            return
        self._accessibility_enabled.add(hwnd)
        targets = [hwnd]

        @WNDENUMPROC
        def visit(child: int, _: int) -> bool:
            if window_class(child) == "Chrome_RenderWidgetHostHWND":
                targets.append(child)
            return True

        user32.EnumChildWindows(hwnd, visit, 0)
        for target in targets:
            for object_id in (OBJID_CLIENT, UIA_ROOT_OBJECT_ID):
                user32.SendMessageTimeoutW(target, WM_GETOBJECT, 0, object_id, SMTO_ABORTIFHUNG, 500, None)

    def is_ide(self, hwnd: int, config: Config) -> bool:
        return self.exe_name(hwnd) in config.ide_apps

    def is_generating(self, hwnd: int) -> bool:
        """True while the chat in hwnd shows a Stop button."""
        if self._uia is None:
            return False
        uia = self._uia_module
        try:
            self._enable_accessibility(hwnd)
            scope = self._uia.ElementFromHandle(hwnd)
            if self.exe_name(hwnd) in BROWSERS:
                # Search only the page: the browser's own "Stop loading"
                # button lives in the toolbar.
                scope = scope.FindFirst(uia.TreeScope_Descendants, self._document_condition)
                if not scope:
                    return False
            buttons = scope.FindAllBuildCache(
                uia.TreeScope_Descendants, self._button_condition, self._name_cache
            )
            return any(
                is_stop_button(buttons.GetElement(index).CachedName or "")
                for index in range(buttons.Length)
            )
        except (OSError, comtypes.COMError):
            return False

    def describe(self, hwnd: int, config: Config) -> tuple[str, Optional[str]]:
        """Return (window description, matched AI name or None)."""
        if not hwnd:
            return "No window", None
        title = window_title(hwnd)
        exe = self.exe_name(hwnd)
        if exe in config.ai_apps:
            return f"{exe.removesuffix('.exe')} app", exe.removesuffix(".exe")
        if exe in config.ide_apps:
            # Electron apps build their accessibility tree lazily; start now so
            # the Stop button is visible by the time a prompt is sent.
            self._enable_accessibility(hwnd)
            name = IDE_NAMES.get(exe, exe.removesuffix(".exe"))
            return f"{name} (AI chat inside)", name
        if exe in BROWSERS:
            browser = BROWSERS[exe]
            host = host_of(self.browser_url(hwnd, title))
            if host:
                return f"{browser} · {host}", matches_site(host, config.ai_sites)
            lowered = title.lower()
            hint = next((hint for hint in AI_TITLE_HINTS if hint in lowered), None)
            return f"{browser} · {title[:40]}", hint
        return (exe.removesuffix(".exe") or title[:40] or "Unknown window"), None


# --------------------------------------------------------------------------
# macOS backend (beta): the same functions as above, built on Apple's APIs.
# A window "handle" on macOS is the app's process id.
# --------------------------------------------------------------------------

if IS_MAC:
    import fcntl
    import plistlib
    import socket
    from collections import deque

    import AppKit
    import ApplicationServices as AX
    import Quartz

    MAC_SHOW_PORT = 47613
    LAUNCH_AGENT_LABEL = "com.edusr9.always-learn"
    LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    KEYCODE_RETURN, KEYCODE_KEYPAD_ENTER, KEYCODE_S = 36, 76, 1
    _CHROMIUM_URL = 'tell application "{}" to return URL of active tab of front window'
    # Browser app name (lowercase) -> (label, AppleScript returning the front tab's URL).
    MAC_BROWSERS = {
        "google chrome": ("Chrome", _CHROMIUM_URL.format("Google Chrome")),
        "arc": ("Arc", _CHROMIUM_URL.format("Arc")),
        "microsoft edge": ("Edge", _CHROMIUM_URL.format("Microsoft Edge")),
        "brave browser": ("Brave", _CHROMIUM_URL.format("Brave Browser")),
        "vivaldi": ("Vivaldi", _CHROMIUM_URL.format("Vivaldi")),
        "chromium": ("Chromium", _CHROMIUM_URL.format("Chromium")),
        "opera": ("Opera", _CHROMIUM_URL.format("Opera")),
        "safari": ("Safari", 'tell application "Safari" to return URL of front document'),
        "firefox": ("Firefox", None),  # no AppleScript URL support: window title only
        "zen": ("Zen", None),
    }
    _instance_lock = None

    def _running_app(pid: int):
        return AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)

    def app_name(pid: int) -> str:
        app = _running_app(pid) if pid else None
        return str(app.localizedName() or "") if app else ""

    def foreground_window() -> Optional[int]:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app else None

    def window_pid(handle: int) -> int:
        return handle

    def window_exists(handle: int) -> bool:
        app = _running_app(handle)
        return app is not None and not app.isTerminated()

    def find_anki_window() -> Optional[int]:
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            bundle = str(app.bundleIdentifier() or "").lower()
            if str(app.localizedName() or "") == "Anki" or "ankiweb" in bundle:
                return int(app.processIdentifier())
        return None

    def _ax_attribute(element, attribute):
        error, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
        return value if error == AX.kAXErrorSuccess else None

    def bring_window_to_front(handle: int) -> None:
        app = _running_app(handle)
        if app is None:
            return
        app.unhide()
        for window in _ax_attribute(AX.AXUIElementCreateApplication(handle), AX.kAXWindowsAttribute) or []:
            AX.AXUIElementSetAttributeValue(window, AX.kAXMinimizedAttribute, False)
        app.activateWithOptions_(
            AppKit.NSApplicationActivateIgnoringOtherApps | AppKit.NSApplicationActivateAllWindows
        )
        deadline = time.monotonic() + 0.4
        while foreground_window() != handle and time.monotonic() < deadline:
            time.sleep(0.05)
        if foreground_window() != handle and app.bundleURL() is not None:
            # Recent macOS versions may ignore activation requests from
            # background apps; `open` on the app always brings it forward.
            subprocess.run(["open", str(app.bundleURL().path())], check=False)
            deadline = time.monotonic() + 1.5
            while foreground_window() != handle and time.monotonic() < deadline:
                time.sleep(0.05)

    def minimize_window(handle: int) -> None:
        app = _running_app(handle)
        if app is not None:
            app.hide()  # like Cmd+H: Anki keeps its card, ready for next time

    def press_study_now(anki_window: int) -> None:
        if foreground_window() != anki_window:
            raise RuntimeError("macOS did not let Anki come to the front, so Study Now was not pressed.")
        for key_down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, KEYCODE_S, key_down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def acquire_single_instance() -> bool:
        global _instance_lock
        _instance_lock = open(Path.home() / ".always-learn.lock", "w")
        try:
            fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def ask_running_instance_to_show() -> None:
        try:
            with socket.create_connection(("127.0.0.1", MAC_SHOW_PORT), timeout=1) as connection:
                connection.sendall(b"show")
        except OSError:
            pass

    def listen_for_show_requests(on_request: Callable[[], None]) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", MAC_SHOW_PORT))
        except OSError:
            return
        server.listen()

        def accept() -> None:
            while True:
                connection, _ = server.accept()
                connection.close()
                on_request()

        threading.Thread(target=accept, daemon=True).start()

    def launch_target() -> tuple[str, list[str]]:
        if FROZEN:
            return sys.executable, []
        return sys.executable, [str(Path(__file__).resolve())]

    def starts_with_windows() -> bool:
        return LAUNCH_AGENT.exists()

    def set_start_with_windows(enabled: bool) -> None:
        if not enabled:
            LAUNCH_AGENT.unlink(missing_ok=True)
            return
        program, arguments = launch_target()
        LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
        with open(LAUNCH_AGENT, "wb") as plist:
            plistlib.dump({
                "Label": LAUNCH_AGENT_LABEL,
                "ProgramArguments": [program, *arguments, STARTUP_FLAG],
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            }, plist)

    def create_desktop_shortcut() -> Optional[Path]:
        return None  # on macOS the app lives in Applications and the Dock

    def find_anki_executable(config: Config) -> Optional[Path]:
        candidates = [Path(config.anki_path)] if config.anki_path else []
        candidates += [Path("/Applications/Anki.app"), Path.home() / "Applications" / "Anki.app"]
        return next((path for path in candidates if path.exists()), None)

    def launch_anki(config: Config, background: bool = False) -> None:
        app = find_anki_executable(config)
        command = ["open", *(["-g", "-j"] if background else []), "-a", str(app) if app else "Anki"]
        if subprocess.run(command, capture_output=True).returncode != 0:
            raise RuntimeError(
                "Could not find Anki. Install it in Applications, or set "
                f'"anki_path" in {CONFIG_PATH}.'
            )

    def anki_process_running() -> bool:
        return find_anki_window() is not None  # checks running apps, not windows

    def start_anki_in_background(config: Config) -> None:
        try:
            ensure_anki_running(config, background=True)  # `open -g -j` starts it hidden
        except RuntimeError as exc:
            print(f"Always Learn: {exc}", file=sys.stderr)

    def missing_permissions() -> list[str]:
        """Ask macOS for the permissions Always Learn needs; return the ones still missing."""
        missing = []
        if not AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True}):
            missing.append("Accessibility")
        preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)
        if preflight is not None and not preflight():
            getattr(Quartz, "CGRequestListenEventAccess", lambda: None)()
            missing.append("Input Monitoring")
        return missing

    class InputHooks:
        """Reports Enter presses and left clicks (never what is typed)."""

        def __init__(
            self, on_enter: Callable[[int], None], on_click: Callable[[int], None],
            on_error: Callable[[str], None],
        ) -> None:
            self.on_enter, self.on_click, self.on_error = on_enter, on_click, on_error
            self._run_loop = None
            self._tap = None

        def start(self) -> None:
            threading.Thread(target=self._listen, daemon=True).start()

        def stop(self) -> None:
            if self._run_loop is not None:
                Quartz.CFRunLoopStop(self._run_loop)

        def _on_event(self, proxy, event_type, event, refcon):
            if event_type in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(self._tap, True)
            elif event_type == Quartz.kCGEventKeyDown:
                keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
                shift = Quartz.CGEventGetFlags(event) & Quartz.kCGEventFlagMaskShift
                # Shift+Enter inserts a newline instead of sending.
                if keycode in (KEYCODE_RETURN, KEYCODE_KEYPAD_ENTER) and not repeat and not shift:
                    self.on_enter(foreground_window())
            elif event_type == Quartz.kCGEventLeftMouseDown:
                self.on_click(foreground_window())
            return event

        def _listen(self) -> None:
            mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown)
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly, mask, self._on_event, None,
            )
            if not self._tap:
                self.on_error(
                    "Allow Always Learn in System Settings → Privacy & Security → "
                    "Input Monitoring, then restart it."
                )
                return
            source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._run_loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self._run_loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap, True)
            Quartz.CFRunLoopRun()

    class AIWindowDetector:
        """macOS: identifies AI chats by browser URL (AppleScript) or app name."""

        MAX_NODES = 8000
        SCAN_SECONDS = 0.6

        def __init__(self) -> None:
            self._urls: dict[int, tuple[str, float]] = {}
            self._accessibility_enabled: set[int] = set()

        def exe_name(self, pid: int) -> str:
            return app_name(pid).lower()

        def is_ide(self, pid: int, config: Config) -> bool:
            return self.exe_name(pid) in config.ide_apps

        def browser_url(self, pid: int, name: str) -> str:
            script = MAC_BROWSERS[name][1]
            if not script:
                return ""
            cached = self._urls.get(pid)
            if cached and time.monotonic() - cached[1] < 1.5:
                return cached[0]
            try:
                url = subprocess.run(
                    ["osascript", "-e", script], capture_output=True, text=True, timeout=2
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                url = ""
            self._urls[pid] = (url, time.monotonic())
            return url

        def _window_title(self, pid: int) -> str:
            window = _ax_attribute(AX.AXUIElementCreateApplication(pid), AX.kAXFocusedWindowAttribute)
            title = _ax_attribute(window, AX.kAXTitleAttribute) if window is not None else None
            return str(title or "")

        def describe(self, pid: int, config: Config) -> tuple[str, Optional[str]]:
            if not pid:
                return "No window", None
            name = self.exe_name(pid)
            if name in config.ai_apps:
                return f"{app_name(pid)} app", name
            if name in config.ide_apps:
                label = IDE_NAMES.get(name, app_name(pid))
                return f"{label} (AI chat inside)", label
            if name in MAC_BROWSERS:
                browser = MAC_BROWSERS[name][0]
                host = host_of(self.browser_url(pid, name))
                if host:
                    return f"{browser} · {host}", matches_site(host, config.ai_sites)
                title = self._window_title(pid).lower()
                return browser, next((hint for hint in AI_TITLE_HINTS if hint in title), None)
            return app_name(pid) or "Unknown app", None

        def _enable_accessibility(self, pid: int, app_element) -> None:
            """Chromium shows web pages to the Accessibility API only when asked;
            Electron apps (VS Code, Cursor, Claude) use AXManualAccessibility."""
            if pid in self._accessibility_enabled:
                return
            self._accessibility_enabled.add(pid)
            AX.AXUIElementSetAttributeValue(app_element, "AXManualAccessibility", True)
            if self.exe_name(pid) in MAC_BROWSERS:
                AX.AXUIElementSetAttributeValue(app_element, "AXEnhancedUserInterface", True)

        def is_generating(self, pid: int) -> bool:
            """True while the app's front window shows the AI's Stop button."""
            app_element = AX.AXUIElementCreateApplication(pid)
            self._enable_accessibility(pid, app_element)
            windows = _ax_attribute(app_element, AX.kAXWindowsAttribute)
            if not windows:
                return False
            # In browsers only look inside the page (the toolbar has its own
            # "Stop loading" button).
            in_browser = self.exe_name(pid) in MAC_BROWSERS
            pending = deque([(windows[0], not in_browser)])
            attributes = [AX.kAXRoleAttribute, AX.kAXTitleAttribute, AX.kAXDescriptionAttribute, AX.kAXChildrenAttribute]
            deadline = time.monotonic() + self.SCAN_SECONDS
            visited = 0
            while pending and visited < self.MAX_NODES and time.monotonic() < deadline:
                element, in_page = pending.popleft()
                visited += 1
                error, values = AX.AXUIElementCopyMultipleAttributeValues(element, attributes, 0, None)
                if error != AX.kAXErrorSuccess or not values:
                    continue
                role, title, description, children = (list(values) + [None] * 4)[:4]
                role = role if isinstance(role, str) else ""
                in_page = in_page or role == "AXWebArea"
                if in_page and role == "AXButton":
                    for label in (title, description):
                        if isinstance(label, str) and is_stop_button(label):
                            return True
                if children is not None and not isinstance(children, str):
                    try:
                        pending.extend((child, in_page) for child in children)
                    except TypeError:
                        pass
            return False


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------


@dataclass
class StudySession:
    """Anki was opened because the AI in ai_window started answering."""

    ai_window: int
    started: float
    seen_generating: bool = False
    quiet_since: Optional[float] = None
    next_check: float = 0.0


class Monitor:
    def __init__(self, config: Config, on_event: Callable[[str, str], None]) -> None:
        self.config = config
        self.on_event = on_event
        self.stop_event = threading.Event()
        self.current_window = "Waiting for the first window…"
        self.current_is_ai = False
        self._enter_presses: "queue.Queue[int]" = queue.Queue()
        self._clicks: "queue.Queue[int]" = queue.Queue()
        self._launch_lock = threading.Lock()
        self._last_trigger = 0.0
        self._last_deck: Optional[str] = None
        self._session: Optional[StudySession] = None
        self._hooks = InputHooks(
            self._enter_presses.put, self._clicks.put, lambda message: self.on_event("error", message)
        )

    def start(self) -> None:
        if sys.platform not in ("win32", "darwin"):
            return
        self._hooks.start()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        self._hooks.stop()

    def _run(self) -> None:
        detector = AIWindowDetector()
        own_pid = os.getpid()
        next_generation_scan = 0.0
        # A click or IDE Enter that may have sent a prompt; confirmed once the
        # AI's Stop button appears shortly after.
        pending: tuple[Optional[int], float] = (None, 0.0)

        def arm(hwnd: int) -> tuple[Optional[int], float]:
            # If the AI was already answering, this press didn't start it.
            if self._session is None and not detector.is_generating(hwnd):
                return hwnd, time.monotonic()
            return pending

        while not self.stop_event.is_set():
            try:
                hwnd = self._enter_presses.get(timeout=0.25)
            except queue.Empty:
                hwnd = None
            now = time.monotonic()
            if hwnd:
                _, ai_name = detector.describe(hwnd, self.config)
                if ai_name and detector.is_ide(hwnd, self.config):
                    pending = arm(hwnd)  # Enter is also used for coding
                elif ai_name:
                    self._start_session(hwnd, f"prompt sent in {ai_name}", PROMPT_SEND_DELAY_SECONDS)
            while not self._clicks.empty():
                clicked = self._clicks.get_nowait()
                if detector.describe(clicked, self.config)[1]:
                    pending = arm(clicked)

            foreground = foreground_window()
            if foreground and window_pid(foreground) != own_pid:
                description, ai_name = detector.describe(foreground, self.config)
                self.current_window = description
                self.current_is_ai = ai_name is not None
                armed_here = pending[0] == foreground and now - pending[1] < CLICK_SEND_WINDOW_SECONDS
                if ai_name and armed_here and self._session is None and now >= next_generation_scan:
                    # The user's click/Enter followed by the AI starting to
                    # answer. The AI resuming on its own between steps is not
                    # a new prompt, so it never reopens Anki.
                    next_generation_scan = now + 0.4
                    if detector.is_generating(foreground):
                        pending = (None, 0.0)
                        self._start_session(foreground, f"prompt sent in {ai_name}")

            if self._session is not None and now >= self._session.next_check:
                self._update_session(detector, now)

    def _start_session(self, ai_window: int, reason: str, delay: float = 0) -> None:
        if self.trigger(reason, delay):
            self._session = StudySession(ai_window, time.monotonic())

    def _update_session(self, detector: AIWindowDetector, now: float) -> None:
        session = self._session
        session.next_check = now + 0.4
        if not window_exists(session.ai_window):
            self._session = None
            return
        if detector.is_generating(session.ai_window):
            session.seen_generating = True
            session.quiet_since = None
            return
        if not session.seen_generating:
            if now - session.started > GENERATION_START_TIMEOUT_SECONDS:
                self._session = None  # could not see the AI working; leave Anki alone
            return
        if session.quiet_since is None:
            session.quiet_since = now
        if now - session.quiet_since < GENERATION_END_GRACE_SECONDS or self._launch_lock.locked():
            return
        self._session = None
        if self.config.return_when_done:
            self._return_to_ai(session.ai_window)

    def _return_to_ai(self, ai_window: int) -> None:
        anki_window = find_anki_window()
        if not anki_window or foreground_window() != anki_window:
            return  # the user already moved on; don't yank windows around
        minimize_window(anki_window)
        bring_window_to_front(ai_window)
        self.on_event("info", f"{time.strftime('%H:%M')}  ·  AI finished, back to the chat")

    def trigger(self, reason: str, delay: float = 0) -> bool:
        if not self.config.enabled:
            return False
        now = time.monotonic()
        if now - self._last_trigger < self.config.cooldown_seconds:
            return False
        self._last_trigger = now
        self.open_anki(reason, delay)
        return True

    def open_anki(self, reason: str, delay: float = 0) -> None:
        def work() -> None:
            if not self._launch_lock.acquire(blocking=False):
                return  # a launch is already in progress
            try:
                time.sleep(delay)
                deck = choose_deck(self.config, self._last_deck)
                self._last_deck = deck
                self.on_event("busy", f"Opening {deck}…")
                open_anki_deck(deck, self.config)
                self.on_event("ok", f"{time.strftime('%H:%M')}  ·  {deck}")
            except (ValueError, RuntimeError) as exc:
                self.on_event("error", str(exc))
            finally:
                self._launch_lock.release()

        threading.Thread(target=work, daemon=True).start()


# --------------------------------------------------------------------------
# Settings window
# --------------------------------------------------------------------------


def wrap_to_width(label: tk.Label, container: tk.Widget, margin: int) -> None:
    """Keep a label's text wrapped to its container as the window resizes."""
    container.bind(
        "<Configure>",
        lambda event: label.configure(wraplength=max(event.width - margin, 60)),
        add="+",
    )


class ToggleRow(tk.Frame):
    """A card with a title, a hint and an on/off switch."""

    def __init__(
        self, parent: tk.Widget, title: str, subtitle: str,
        variable: tk.BooleanVar, scale: float,
    ) -> None:
        super().__init__(
            parent, bg=COLORS["surface"], highlightthickness=1,
            highlightbackground=COLORS["border"], padx=12, pady=9, cursor="hand2",
        )
        self.variable = variable
        self.width, self.height = round(42 * scale), round(24 * scale)
        self.switch = tk.Canvas(
            self, width=self.width, height=self.height, bg=COLORS["surface"],
            highlightthickness=0, cursor="hand2",
        )
        self.switch.pack(side="right", padx=(12, 0))
        text = tk.Frame(self, bg=COLORS["surface"])
        text.pack(side="left", fill="x", expand=True)
        title_label = tk.Label(
            text, text=title, font=FONT_BOLD, bg=COLORS["surface"], fg=COLORS["text"],
            anchor="w", cursor="hand2",
        )
        title_label.pack(fill="x")
        subtitle_label = tk.Label(
            text, text=subtitle, font=FONT_SMALL, bg=COLORS["surface"], fg=COLORS["muted"],
            anchor="w", justify="left", cursor="hand2",
        )
        subtitle_label.pack(fill="x", pady=(1, 0))
        wrap_to_width(subtitle_label, text, 8)
        for widget in (self, self.switch, text, title_label, subtitle_label):
            widget.bind("<Button-1>", lambda _: variable.set(not variable.get()))
        variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    def draw(self) -> None:
        if not hasattr(self, "_images"):
            self._images = {on: ImageTk.PhotoImage(switch_image(on, self.width, self.height)) for on in (True, False)}
            self.switch.delete("all")
            self._switch_item = self.switch.create_image(0, 0, anchor="nw")
        self.switch.itemconfigure(self._switch_item, image=self._images[bool(self.variable.get())])


def hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def rounded_image(
    width: int, height: int, radius: int, top: str, bottom: Optional[str] = None,
    border: Optional[str] = None, shadow: int = 0,
) -> Image.Image:
    """An anti-aliased rounded rectangle with a vertical gradient, optional border
    and soft drop shadow (drawn 4x larger, then scaled down for smooth edges)."""
    ss = 4
    full_w, full_h = (width + 2 * shadow) * ss, (height + 2 * shadow) * ss
    image = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))
    box = (shadow * ss, shadow * ss, (shadow + width) * ss - 1, (shadow + height) * ss - 1)
    if shadow:
        shade = Image.new("RGBA", (full_w, full_h), (0, 0, 0, 0))
        drop = shadow * ss // 2
        ImageDraw.Draw(shade).rounded_rectangle(
            (box[0], box[1] + drop, box[2], box[3] + drop), radius * ss, fill=(0, 0, 0, 120)
        )
        image = shade.filter(ImageFilter.GaussianBlur(shadow * ss / 2))
    top_rgb, bottom_rgb = hex_rgb(top), hex_rgb(bottom or top)
    gradient = Image.new("RGBA", (1, height * ss))
    for y in range(height * ss):
        mix = y / max(1, height * ss - 1)
        gradient.putpixel((0, y), tuple(round(a + (b - a) * mix) for a, b in zip(top_rgb, bottom_rgb)) + (255,))
    gradient = gradient.resize((width * ss, height * ss))
    mask = Image.new("L", (width * ss, height * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width * ss - 1, height * ss - 1), radius * ss, fill=255)
    image.paste(gradient, (shadow * ss, shadow * ss), mask)
    if border:
        ImageDraw.Draw(image).rounded_rectangle(box, radius * ss, outline=hex_rgb(border) + (255,), width=ss)
    return image.resize((width + 2 * shadow, height + 2 * shadow), Image.LANCZOS)


def switch_image(on: bool, width: int, height: int) -> Image.Image:
    if on:
        track = rounded_image(width, height, height // 2, "#62B4FF", "#3B8CF0")
    else:
        track = rounded_image(width, height, height // 2, "#2A3342", "#222936", border="#3A4760")
    inset = max(2, height // 7)
    size = height - 2 * inset
    knob = rounded_image(size, size, size // 2, "#FFFFFF", "#DDE3EC")
    track.alpha_composite(knob, (width - size - inset if on else inset, inset))
    return track


class RoundedButton(tk.Canvas):
    """A pill-shaped button with gradient, soft shadow, and hover/press states."""

    STYLES = {
        "primary": {
            "normal": ("#6CBBFF", "#3B8CF0", None), "hover": ("#8CCBFF", "#509CF6", None),
            "pressed": ("#3B8CF0", "#2E74D0", None), "text": COLORS["on_blue"],
        },
        "secondary": {
            "normal": ("#2C3546", "#212836", "#3A4760"), "hover": ("#36425A", "#29323F", "#4C5D7D"),
            "pressed": ("#1F2633", "#1A202B", "#3A4760"), "text": COLORS["text"],
        },
    }

    def __init__(
        self, parent: tk.Widget, text: str, command: Callable[[], None], scale: float,
        variant: str = "primary", min_text: str = "",
    ) -> None:
        style = self.STYLES[variant]
        font = tkfont.Font(root=parent, font=FONT_BOLD)
        shadow = max(2, round((4 if variant == "primary" else 2) * scale))
        height = round(36 * scale)
        width = max(font.measure(text), font.measure(min_text)) + round(40 * scale)
        super().__init__(
            parent, width=width + 2 * shadow, height=height + 2 * shadow,
            bg=parent.cget("bg"), highlightthickness=0, bd=0, cursor="hand2",
        )
        self.command = command
        self._images = {
            state: ImageTk.PhotoImage(rounded_image(width, height, height // 2, *style[state][:2],
                                                    border=style[state][2], shadow=shadow))
            for state in ("normal", "hover", "pressed")
        }
        self._background = self.create_image(0, 0, anchor="nw", image=self._images["normal"])
        self._text = self.create_text(
            width / 2 + shadow, height / 2 + shadow, text=text, font=FONT_BOLD, fill=style["text"]
        )
        self._inside = False
        self.bind("<Enter>", lambda _: self._set_inside(True))
        self.bind("<Leave>", lambda _: self._set_inside(False))
        self.bind("<ButtonPress-1>", lambda _: self._show("pressed"))
        self.bind("<ButtonRelease-1>", self._release)

    def _show(self, state: str) -> None:
        self.itemconfigure(self._background, image=self._images[state])

    def _set_inside(self, inside: bool) -> None:
        self._inside = inside
        self._show("hover" if inside else "normal")

    def _release(self, _: tk.Event) -> None:
        self._show("hover" if self._inside else "normal")
        if self._inside:
            self.command()

    def set_text(self, text: str) -> None:
        self.itemconfigure(self._text, text=text)


class SegmentedControl(tk.Canvas):
    """A pill-shaped choice between options; the highlight slides to the selection."""

    def __init__(
        self, parent: tk.Widget, options: list[tuple[str, str]], variable: tk.StringVar, scale: float,
    ) -> None:
        self.bar_height = round(40 * scale)
        super().__init__(
            parent, height=self.bar_height, bg=parent.cget("bg"),
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self.options = options
        self.variable = variable
        self.inset = max(3, round(4 * scale))
        self._images: dict[str, ImageTk.PhotoImage] = {}
        self._size = (0, 0)
        self._pill_x: Optional[float] = None
        self._animation: Optional[str] = None
        self.bind("<Configure>", lambda _: self._rebuild())
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._slide())

    def _selected_index(self) -> int:
        values = [value for value, _ in self.options]
        return values.index(self.variable.get()) if self.variable.get() in values else 0

    def _segment_width(self) -> float:
        return (self.winfo_width() - 2 * self.inset) / len(self.options)

    def _click(self, event: tk.Event) -> None:
        index = int((event.x - self.inset) // max(1, self._segment_width()))
        index = min(max(index, 0), len(self.options) - 1)
        self.variable.set(self.options[index][0])

    def _rebuild(self) -> None:
        width, height = self.winfo_width(), self.bar_height
        if width < 20 or (width, height) == self._size:
            return
        self._size = (width, height)
        pill_w, pill_h = round(self._segment_width()), height - 2 * self.inset
        self._images = {
            "track": ImageTk.PhotoImage(rounded_image(width, height, height // 2, "#1A202B", "#151A23", border=COLORS["border"])),
            "pill": ImageTk.PhotoImage(rounded_image(pill_w, pill_h, pill_h // 2, "#6CBBFF", "#3B8CF0")),
        }
        self._pill_x = self.inset + self._selected_index() * self._segment_width()
        self._draw()

    def _draw(self) -> None:
        if not self._images:
            return
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._images["track"])
        self.create_image(self._pill_x, self.inset, anchor="nw", image=self._images["pill"])
        segment = self._segment_width()
        selected = self._selected_index()
        for index, (_, text) in enumerate(self.options):
            self.create_text(
                self.inset + segment * (index + 0.5), self.bar_height / 2, text=text, font=FONT_BOLD,
                fill=COLORS["on_blue"] if index == selected else COLORS["muted"],
            )

    def _slide(self) -> None:
        if self._pill_x is None:
            return
        target = self.inset + self._selected_index() * self._segment_width()
        if self._animation:
            self.after_cancel(self._animation)
        start = self._pill_x

        def step(frame: int = 1, frames: int = 8) -> None:
            progress = 1 - (1 - frame / frames) ** 3  # ease out
            self._pill_x = start + (target - start) * progress
            self._draw()
            self._animation = self.after(15, step, frame + 1) if frame < frames else None

        step()


class ScrollArea(tk.Frame):
    """A vertically scrollable area whose scrollbar only shows when needed."""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, bg=COLORS["background"])
        self.canvas = tk.Canvas(self, bg=COLORS["background"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=COLORS["background"])
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self.canvas.yview, style="Dark.Vertical.TScrollbar"
        )
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.content.bind(
            "<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.bind(
            "<Configure>", lambda event: self.canvas.itemconfigure(self._window, width=event.width)
        )
        self.bind_all("<MouseWheel>", self._on_wheel)

    def _on_scroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        # Overlay the scrollbar so showing it never changes the content width.
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.place_forget()
        else:
            self.scrollbar.place(relx=1, rely=0, relheight=1, anchor="ne")

    def _on_wheel(self, event: tk.Event) -> None:
        if isinstance(event.widget, (str, tk.Listbox)):
            return  # lists scroll themselves
        if self.scrollbar.winfo_ismapped():
            # Windows reports wheel steps of 120; macOS trackpads report small deltas.
            step = -event.delta if IS_MAC else int(-event.delta / 120)
            self.canvas.yview_scroll(step, "units")


class SettingsWindow:
    WIDTH = 440

    def __init__(self, root: tk.Tk, config: Config, monitor: Monitor) -> None:
        self.root = root
        self.config = config
        self.monitor = monitor
        self.tray_icon: Optional[Icon] = None
        self.events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        # macOS reports 72 dpi and scales Retina itself; Windows needs DPI scaling.
        self.scale = 1.0 if IS_MAC else root.winfo_fpixels("1i") / 96

        root.title(APP_NAME)
        root.configure(bg=COLORS["background"])
        root.minsize(self.px(340), self.px(300))
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._style_ttk()
        self._set_window_icon()

        scroll = ScrollArea(root)
        scroll.pack(fill="both", expand=True)
        page = tk.Frame(scroll.content, bg=COLORS["background"], padx=self.px(18), pady=self.px(16))
        page.pack(fill="both", expand=True)

        self._build_header(page)
        self._build_status(page)

        self.mode = tk.StringVar(value=config.mode)
        self._section(page, "DECK")
        SegmentedControl(page, [("configured", "One deck"), ("random", "Random")], self.mode, self.scale).pack(fill="x")
        self.mode.trace_add("write", lambda *_: self._on_mode_change())
        self._build_deck_pickers(page)

        self._section(page, "OPTIONS")
        self.return_when_done = tk.BooleanVar(value=config.return_when_done)
        ToggleRow(
            page, "Hide Anki when the AI is done",
            "Brings your chat back as soon as the answer is ready.",
            self.return_when_done, self.scale,
        ).pack(fill="x")
        self.return_when_done.trace_add("write", lambda *_: self.persist())
        self.sound_on_open = tk.BooleanVar(value=config.sound_on_open)
        ToggleRow(
            page, "Play a sound when Anki opens",
            "A short chime as your card pops up.",
            self.sound_on_open, self.scale,
        ).pack(fill="x", pady=(self.px(8), 0))
        self.sound_on_open.trace_add("write", lambda *_: self._on_sound_toggle())
        self.start_with_windows = tk.BooleanVar(value=starts_with_windows())
        ToggleRow(
            page, START_AT_LOGIN_LABEL,
            "Runs in the tray and opens Anki in the background when you log in.",
            self.start_with_windows, self.scale,
        ).pack(fill="x", pady=(self.px(8), 0))
        self.start_with_windows.trace_add(
            "write",
            lambda *_: threading.Thread(
                target=set_start_with_windows, args=(self.start_with_windows.get(),), daemon=True
            ).start(),
        )

        footer = tk.Frame(page, bg=COLORS["background"])
        footer.pack(fill="x", pady=(self.px(16), 0))
        RoundedButton(footer, "Try it: open Anki now", self.test_now, self.scale).pack(side="left")
        tk.Label(
            footer, text="Saved automatically", font=FONT_SMALL,
            fg=COLORS["muted"], bg=COLORS["background"],
        ).pack(side="right")

        self._on_mode_change()
        self.refresh_decks()
        root.geometry(f"{self.px(self.WIDTH)}x{self.px(600)}")
        page.bind("<Configure>", lambda _: self._fit_height(page), add="+")
        self._poll()

    def px(self, value: float) -> int:
        return int(value * self.scale)

    # ---- construction helpers -------------------------------------------

    def _style_ttk(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["surface_high"], background=COLORS["surface_high"],
            foreground=COLORS["text"], arrowcolor=COLORS["text"],
            bordercolor=COLORS["border"], lightcolor=COLORS["surface_high"],
            darkcolor=COLORS["surface_high"], insertcolor=COLORS["text"],
            selectbackground=COLORS["surface_high"], selectforeground=COLORS["text"],
            padding=self.px(5),
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", COLORS["surface_high"])],
            background=[("active", COLORS["border"])],
            bordercolor=[("focus", COLORS["blue"])],
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=COLORS["border"], troughcolor=COLORS["background"],
            bordercolor=COLORS["background"], lightcolor=COLORS["border"],
            darkcolor=COLORS["border"], arrowcolor=COLORS["muted"], gripcount=0,
        )
        style.map("Dark.Vertical.TScrollbar", background=[("active", COLORS["muted"])])
        self.root.option_add("*TCombobox*Listbox.background", COLORS["surface_high"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["blue"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["on_blue"])
        self.root.option_add("*TCombobox*Listbox.font", FONT)

    def _set_window_icon(self) -> None:
        try:
            self.window_icon = ImageTk.PhotoImage(Image.open(ICON_PATH).resize((64, 64)))
            self.root.iconphoto(True, self.window_icon)
        except OSError:
            pass

    def _build_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=COLORS["background"])
        header.pack(fill="x", pady=(0, self.px(14)))
        try:
            size = self.px(46)
            self.header_icon = ImageTk.PhotoImage(Image.open(ICON_PATH).resize((size, size), Image.LANCZOS))
            tk.Label(header, image=self.header_icon, bg=COLORS["background"]).pack(side="left")
        except OSError:
            pass
        text = tk.Frame(header, bg=COLORS["background"])
        text.pack(side="left", padx=(self.px(12), 0))
        tk.Label(
            text, text=APP_NAME, font=FONT_TITLE, fg=COLORS["text"], bg=COLORS["background"], anchor="w"
        ).pack(fill="x")
        tk.Label(
            text, text="Anki pops up while the AI is thinking.", font=FONT_SMALL,
            fg=COLORS["muted"], bg=COLORS["background"], anchor="w",
        ).pack(fill="x")

    def _fit_height(self, page: tk.Frame) -> None:
        """Grow or shrink the window to fit its content, within the screen."""
        height = min(page.winfo_reqheight(), self.root.winfo_screenheight() - self.px(120))
        mapped = self.root.winfo_ismapped()
        if not mapped or height != self.root.winfo_height():
            width = self.root.winfo_width() if mapped else self.px(self.WIDTH)
            self.root.geometry(f"{width}x{height}")

    def _section(self, parent: tk.Widget, text: str) -> None:
        tk.Label(
            parent, text=text, font=FONT_SECTION, fg=COLORS["muted"],
            bg=COLORS["background"], anchor="w",
        ).pack(fill="x", pady=(self.px(16), self.px(6)))

    def _build_status(self, parent: tk.Widget) -> None:
        card = tk.Frame(
            parent, bg=COLORS["surface"], padx=self.px(14), pady=self.px(11),
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        card.pack(fill="x")
        size = self.px(11)
        self.status_dot = tk.Canvas(
            card, width=size, height=size, bg=COLORS["surface"], highlightthickness=0
        )
        self.status_dot.grid(row=0, column=0, padx=(0, self.px(9)))
        self.status_title = tk.Label(card, font=FONT_STATUS, bg=COLORS["surface"], fg=COLORS["text"], anchor="w")
        self.status_title.grid(row=0, column=1, sticky="w")
        text_margin = self.px(130)
        self.status_detail = tk.Label(
            card, font=FONT_SMALL, bg=COLORS["surface"], fg=COLORS["muted"], anchor="w", justify="left"
        )
        self.status_detail.grid(row=1, column=1, sticky="w")
        wrap_to_width(self.status_detail, card, text_margin)
        self.last_event = tk.Label(
            card, font=FONT_SMALL, bg=COLORS["surface"], fg=COLORS["muted"], anchor="w", justify="left"
        )
        self.last_event.grid(row=2, column=1, sticky="w", pady=(self.px(2), 0))
        self.last_event.grid_remove()  # shown once something has happened
        wrap_to_width(self.last_event, card, text_margin)
        card.columnconfigure(1, weight=1)
        self.toggle_button = RoundedButton(
            card, "Pause", lambda: self.set_monitoring(not self.config.enabled), self.scale,
            variant="secondary", min_text="Resume",
        )
        self.toggle_button.grid(row=0, column=2, rowspan=3, sticky="e")
        self._render_status()

    def _build_deck_pickers(self, parent: tk.Widget) -> None:
        self.deck_area = tk.Frame(parent, bg=COLORS["background"])
        self.deck_area.pack(fill="x", pady=(self.px(8), 0))

        self.single_picker = tk.Frame(self.deck_area, bg=COLORS["background"])
        self.configured_deck = tk.StringVar(value=self.config.configured_deck)
        self.deck_combo = ttk.Combobox(
            self.single_picker, textvariable=self.configured_deck,
            style="Dark.TCombobox", font=FONT,
        )
        self.deck_combo.pack(side="left", fill="x", expand=True)
        self.configured_deck.trace_add("write", lambda *_: self.persist())

        self.random_picker = tk.Frame(self.deck_area, bg=COLORS["background"])
        self.deck_names: list[str] = []
        self.ticked_decks = set(self.config.random_decks)

        # Presets: save the ticked decks under a name, pick it again later.
        preset_row = tk.Frame(self.random_picker, bg=COLORS["background"])
        preset_row.pack(fill="x", pady=(0, self.px(8)))
        self.preset_name = tk.StringVar(value=self.config.active_preset)
        self.preset_combo = ttk.Combobox(
            preset_row, textvariable=self.preset_name, style="Dark.TCombobox", font=FONT,
            values=sorted(self.config.deck_presets),
        )
        self.preset_combo.pack(side="left", fill="x", expand=True)
        self.preset_combo.bind("<<ComboboxSelected>>", lambda _: self._apply_preset(self.preset_name.get()))
        self.preset_combo.bind("<Return>", lambda _: self._save_preset())
        self.preset_name.trace_add("write", lambda *_: self._update_deck_hint())
        delete = tk.Label(
            preset_row, text="✕", font=FONT_BOLD, cursor="hand2",
            fg=COLORS["muted"], bg=COLORS["background"], padx=self.px(6),
        )
        delete.pack(side="right")
        delete.bind("<Button-1>", lambda _: self._delete_preset())
        RoundedButton(preset_row, "Save preset", self._save_preset, self.scale, variant="secondary").pack(
            side="right", padx=(self.px(6), 0)
        )

        # A checklist: click a deck to tick it, or press and drag over several.
        list_frame = tk.Frame(self.random_picker, bg=COLORS["background"])
        list_frame.pack(fill="x")
        self.random_list = tk.Listbox(
            list_frame, selectmode="single", exportselection=False,
            height=4, font=FONT, activestyle="none", relief="flat", bd=0,
            bg=COLORS["surface_high"], fg=COLORS["text"],
            selectbackground=COLORS["surface_high"], selectforeground=COLORS["text"],
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"], cursor="hand2",
        )
        list_scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.random_list.yview, style="Dark.Vertical.TScrollbar"
        )
        self.random_list.configure(yscrollcommand=list_scrollbar.set)
        list_scrollbar.pack(side="right", fill="y")
        self.random_list.pack(side="left", fill="x", expand=True)
        self._drag_value: Optional[bool] = None
        self._drag_last = 0
        self.random_list.bind("<ButtonPress-1>", self._drag_start)
        self.random_list.bind("<B1-Motion>", self._drag_move)
        self.random_list.bind("<ButtonRelease-1>", self._drag_end)

        quick_row = tk.Frame(self.random_picker, bg=COLORS["background"])
        quick_row.pack(fill="x", pady=(self.px(4), 0))
        for text, ticked in (("Tick all", True), ("Untick all", False)):
            link = tk.Label(
                quick_row, text=text, font=FONT_SMALL, cursor="hand2",
                fg=COLORS["blue"], bg=COLORS["background"],
            )
            link.pack(side="left", padx=(0, self.px(12)))
            link.bind("<Button-1>", lambda _, ticked=ticked: self._set_all_decks(ticked))
        tk.Label(
            quick_row, text="Tip: press and drag to tick several", font=FONT_SMALL,
            fg=COLORS["muted"], bg=COLORS["background"],
        ).pack(side="right")

        hint_row = tk.Frame(self.deck_area, bg=COLORS["background"])
        hint_row.pack(side="bottom", fill="x", pady=(self.px(5), 0))
        self.deck_hint = tk.Label(
            hint_row, font=FONT_SMALL, fg=COLORS["muted"], bg=COLORS["background"], anchor="w"
        )
        self.deck_hint.pack(side="left")
        refresh = tk.Label(
            hint_row, text="↻ Reload", font=FONT_SMALL, cursor="hand2",
            fg=COLORS["blue"], bg=COLORS["background"],
        )
        refresh.pack(side="right")
        refresh.bind("<Button-1>", lambda _: self.refresh_decks())

    # ---- behaviour --------------------------------------------------------

    def _on_sound_toggle(self) -> None:
        self.persist()
        if self.sound_on_open.get():
            play_open_sound()  # let people hear what they turned on

    def _on_mode_change(self) -> None:
        if self.mode.get() == "random":
            self.single_picker.pack_forget()
            self.random_picker.pack(fill="x")
        else:
            self.random_picker.pack_forget()
            self.single_picker.pack(fill="x")
        self.persist()
        self._update_deck_hint()

    def refresh_decks(self) -> None:
        self.deck_hint.configure(text="Loading decks from Anki…", fg=COLORS["muted"])

        def load() -> None:
            try:
                decks = list_anki_decks()
            except (RuntimeError, AnkiUnavailable):
                self.events.put(("decks", ""))
                return
            self.events.put(("decks", json.dumps(decks)))

        threading.Thread(target=load, daemon=True).start()

    def _fill_decks(self, decks: list[str]) -> None:
        self.anki_reachable = bool(decks)
        if not decks:
            decks = sorted(set(self.config.random_decks + [self.config.configured_deck]) - {""})
        self.deck_combo.configure(values=decks)
        self.deck_names = decks
        self.ticked_decks &= set(decks)  # forget decks that no longer exist
        self.random_list.delete(0, "end")
        for index in range(len(decks)):
            self.random_list.insert("end", "")
            self._render_deck_row(index)
        self.random_list.configure(height=min(max(len(decks), 2), 8))
        self._update_deck_hint()

    def _render_deck_row(self, index: int) -> None:
        deck = self.deck_names[index]
        ticked = deck in self.ticked_decks
        self.random_list.delete(index)
        self.random_list.insert(index, f"{'☑' if ticked else '☐'}  {deck}")
        color = COLORS["text"] if ticked else COLORS["muted"]
        self.random_list.itemconfigure(index, fg=color, selectforeground=color)

    def _set_deck(self, index: int, ticked: bool) -> None:
        deck = self.deck_names[index]
        if (deck in self.ticked_decks) != ticked:
            self.ticked_decks ^= {deck}
            self._render_deck_row(index)

    def _drag_start(self, event: tk.Event) -> str:
        """Clicking a deck flips it; dragging applies the same state to every deck passed."""
        if self.deck_names:
            index = self.random_list.nearest(event.y)
            self._drag_value = self.deck_names[index] not in self.ticked_decks
            self._drag_last = index
            self._set_deck(index, self._drag_value)
            self._update_deck_hint()
        return "break"  # no default list selection highlight

    def _drag_move(self, event: tk.Event) -> str:
        if self._drag_value is None:
            return "break"
        if event.y < 0:  # dragging past the edge scrolls the list
            self.random_list.yview_scroll(-1, "units")
        elif event.y > self.random_list.winfo_height():
            self.random_list.yview_scroll(1, "units")
        index = self.random_list.nearest(event.y)
        # Fill every row between the last one and this one (fast drags skip rows).
        for row in range(min(index, self._drag_last), max(index, self._drag_last) + 1):
            self._set_deck(row, self._drag_value)
        self._drag_last = index
        self._update_deck_hint()
        return "break"

    def _drag_end(self, _: tk.Event) -> str:
        if self._drag_value is not None:
            self._drag_value = None
            self.persist()
        return "break"

    def _set_all_decks(self, ticked: bool) -> None:
        for index in range(len(self.deck_names)):
            self._set_deck(index, ticked)
        self.persist()
        self._update_deck_hint()

    def _apply_preset(self, name: str) -> None:
        decks = self.config.deck_presets.get(name)
        if decks is None:
            return
        self.ticked_decks = set(decks) & set(self.deck_names) if self.deck_names else set(decks)
        for index in range(len(self.deck_names)):
            self._render_deck_row(index)
        self.config.active_preset = name
        self.persist()
        self._update_deck_hint()

    def _save_preset(self) -> None:
        name = self.preset_name.get().strip()
        if not name:
            self.deck_hint.configure(text="Type a name for the preset first (e.g. Chinese).", fg=COLORS["yellow"])
            self.preset_combo.focus_set()
            return
        if not self.ticked_decks:
            self.deck_hint.configure(text="Tick the decks for this preset first.", fg=COLORS["yellow"])
            return
        decks = [deck for deck in self.deck_names if deck in self.ticked_decks] or sorted(self.ticked_decks)
        self.config.deck_presets[name] = decks
        self.config.active_preset = name
        self.preset_combo.configure(values=sorted(self.config.deck_presets))
        self.persist()
        self.deck_hint.configure(text=f"Saved preset \u201c{name}\u201d ({len(decks)} decks).", fg=COLORS["green"])

    def _delete_preset(self) -> None:
        name = self.preset_name.get().strip()
        if name not in self.config.deck_presets:
            return
        del self.config.deck_presets[name]
        self.config.active_preset = ""
        self.preset_combo.configure(values=sorted(self.config.deck_presets))
        self.preset_name.set("")
        self.persist()
        self.deck_hint.configure(text=f"Deleted preset \u201c{name}\u201d.", fg=COLORS["muted"])

    def _update_deck_hint(self) -> None:
        if not getattr(self, "anki_reachable", True):
            self.deck_hint.configure(text="Open Anki to load your decks.", fg=COLORS["yellow"])
        elif self.mode.get() != "random":
            self.deck_hint.configure(text=f"{len(self.deck_names)} decks in Anki", fg=COLORS["muted"])
        elif len(self.ticked_decks) < 2:
            self.deck_hint.configure(text="Tick at least 2 decks to rotate between them.", fg=COLORS["yellow"])
        elif self._preset_changed():
            name = self.preset_name.get().strip()
            self.deck_hint.configure(text=f"Changed: click Save preset to update \u201c{name}\u201d.", fg=COLORS["yellow"])
        else:
            self.deck_hint.configure(
                text=f"Random from {len(self.ticked_decks)} of {len(self.deck_names)} decks, never the same twice in a row",
                fg=COLORS["muted"],
            )

    def _preset_changed(self) -> bool:
        name = self.preset_name.get().strip() if hasattr(self, "preset_name") else ""
        saved = self.config.deck_presets.get(name)
        return saved is not None and set(saved) & set(self.deck_names) != self.ticked_decks

    def persist(self) -> None:
        if not hasattr(self, "sound_on_open"):
            return  # still constructing
        self.config.return_when_done = self.return_when_done.get()
        self.config.sound_on_open = self.sound_on_open.get()
        self.config.mode = self.mode.get()
        self.config.configured_deck = self.configured_deck.get().strip()
        if self.deck_names:
            self.config.random_decks = [deck for deck in self.deck_names if deck in self.ticked_decks]
        save_config(self.config)

    def set_monitoring(self, enabled: bool) -> None:
        self.config.enabled = enabled
        save_config(self.config)
        self._render_status()
        if self.tray_icon is not None:
            self.tray_icon.title = f"{APP_NAME} — Active" if enabled else f"{APP_NAME} — Paused"
            self.tray_icon.update_menu()

    def _render_status(self) -> None:
        enabled = self.config.enabled
        color = COLORS["green"] if enabled else COLORS["muted"]
        size = self.px(11)
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, size - 1, size - 1, fill=color, outline=color)
        self.status_title.configure(text="Watching for AI" if enabled else "Paused")
        self.toggle_button.set_text("Pause" if enabled else "Resume")

    def _poll(self) -> None:
        while True:
            try:
                kind, message = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "decks":
                self._fill_decks(json.loads(message) if message else [])
                continue
            self.last_event.grid()
            if kind == "error":
                self.last_event.configure(text=f"⚠  {message}", fg=COLORS["red"])
                if self.tray_icon is not None and self.root.state() == "withdrawn":
                    self.tray_icon.notify(message, APP_NAME)
            elif kind == "busy":
                self.last_event.configure(text=message, fg=COLORS["yellow"])
            elif kind == "info":
                self.last_event.configure(text=message, fg=COLORS["blue"])
            else:
                self.last_event.configure(text=f"Opened  {message}", fg=COLORS["green"])
        marker = "  ✓ AI chat" if self.monitor.current_is_ai else ""
        self.status_detail.configure(
            text=f"Now: {self.monitor.current_window}{marker}",
            fg=COLORS["blue"] if self.monitor.current_is_ai else COLORS["muted"],
        )
        self.root.after(250, self._poll)

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_decks()

    def hide(self) -> None:
        self.root.withdraw()

    def test_now(self) -> None:
        self.persist()
        self.monitor.open_anki("manual test")


def use_dark_title_bar(root: tk.Tk) -> None:
    if sys.platform != "win32":
        return
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id())
    value = ctypes.c_int(1)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        pass


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


# --------------------------------------------------------------------------
# Tray
# --------------------------------------------------------------------------


def create_tray_icon(
    root: tk.Tk, config: Config, settings: SettingsWindow, monitor: Monitor
) -> Icon:
    def open_settings(_: Icon, __: MenuItem) -> None:
        root.after(0, settings.show)

    def toggle_monitoring(_: Icon, __: MenuItem) -> None:
        root.after(0, lambda: settings.set_monitoring(not config.enabled))

    def start_anki(_: Icon, __: MenuItem) -> None:
        root.after(0, settings.test_now)

    def quit_app(icon: Icon, __: MenuItem) -> None:
        monitor.stop()
        icon.stop()
        root.after(0, root.destroy)

    menu = Menu(
        MenuItem(
            lambda _: "●  ACTIVE — watching for AI" if config.enabled else "○  PAUSED — monitoring is off",
            None,
            enabled=False,
        ),
        MenuItem(
            lambda _: "Pause monitoring" if config.enabled else "Resume monitoring",
            toggle_monitoring,
        ),
        Menu.SEPARATOR,
        MenuItem("Open settings", open_settings, default=True),
        MenuItem("Open Anki now", start_anki),
        Menu.SEPARATOR,
        MenuItem(f"Quit {APP_NAME}", quit_app),
    )
    return Icon(
        "always-learn",
        Image.open(ICON_PATH),
        f"{APP_NAME} — Active" if config.enabled else f"{APP_NAME} — Paused",
        menu,
    )


def self_test() -> None:
    """Check the platform layer without showing any UI (used by the CI build)."""
    config = load_config()
    detector = AIWindowDetector()
    foreground = foreground_window()
    print(f"platform: {sys.platform}, frozen: {FROZEN}")
    print(f"foreground: {foreground!r} -> {detector.describe(foreground, config) if foreground else None}")
    print(f"anki window: {find_anki_window()!r}, anki app: {find_anki_executable(config)}")
    print(f"launch target: {launch_target()}")
    print(f"starts at login: {starts_with_windows()}, icon exists: {ICON_PATH.exists()}")
    print(f"stop-button rules ok: {is_stop_button('Stop response') and not is_stop_button('Stop (Shift+F5)')}")
    print("self-test ok")


def main() -> None:
    if "--self-test" in sys.argv:
        self_test()
        return
    at_login = STARTUP_FLAG in sys.argv
    enable_dpi_awareness()
    if not acquire_single_instance():
        if not at_login:
            ask_running_instance_to_show()
        return
    first_run = not CONFIG_PATH.exists()
    config = load_config()
    if first_run:
        try:
            create_desktop_shortcut()
        except (OSError, comtypes.COMError) as exc:
            print(f"Always Learn: could not create a desktop shortcut: {exc}", file=sys.stderr)
    root = tk.Tk()
    settings: Optional[SettingsWindow] = None
    monitor = Monitor(config, lambda kind, message: settings and settings.events.put((kind, message)))
    settings = SettingsWindow(root, config, monitor)
    use_dark_title_bar(root)
    tray_icon = create_tray_icon(root, config, settings, monitor)
    settings.tray_icon = tray_icon
    listen_for_show_requests(lambda: root.after(0, settings.show))
    if not at_login:
        threading.Thread(target=refresh_start_at_login, daemon=True).start()
    if at_login:
        root.withdraw()
        threading.Thread(target=start_anki_in_background, args=(config,), daemon=True).start()
    missing = missing_permissions()
    if missing:
        root.deiconify()
        settings.events.put((
            "error",
            f"macOS needs permission: System Settings → Privacy & Security → "
            f"{' and '.join(missing)} → turn on Always Learn, then restart it.",
        ))
    monitor.start()
    if IS_MAC:
        # macOS menu-bar icons must live on the main thread, next to Tk.
        tray_icon.run_detached()
    else:
        threading.Thread(target=tray_icon.run, daemon=True).start()
    root.mainloop()
    monitor.stop()
    tray_icon.stop()


if __name__ == "__main__":
    main()
