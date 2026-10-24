"""Windows-first utility for starting Anki review during AI usage."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional

try:
    from PIL import Image, ImageTk
    from pystray import Icon, Menu, MenuItem
except ModuleNotFoundError as exc:
    if exc.name not in {"PIL", "pystray"}:
        raise
    raise SystemExit(
        "Always Learn needs its UI dependencies. From this repository folder, "
        "run: python -m pip install -r requirements.txt"
    ) from exc

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
CONFIG_PATH = Path.home() / ".always-learn.json"
STARTUP_FLAG = "--startup"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SHOW_EVENT_NAME = "Local\\always-learn-show-window"
ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_STARTUP_TIMEOUT_SECONDS = 60
PROMPT_SEND_DELAY_SECONDS = 0.4
# How long to wait for the AI's Stop button to appear after a prompt is sent.
GENERATION_START_TIMEOUT_SECONDS = 20
# The Stop button must be gone this long before the answer counts as finished.
GENERATION_END_GRACE_SECONDS = 1.0

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
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI Semibold", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_SECTION = ("Segoe UI Semibold", 8)
FONT_STATUS = ("Segoe UI Semibold", 11)
FONT_TITLE = ("Segoe UI Semibold", 15)

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
DEFAULT_AI_APPS = ["claude.exe", "chatgpt.exe", "perplexity.exe"]
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
)


@dataclass
class Config:
    enabled: bool = True
    # Minimize Anki and go back to the chat once the AI finishes answering.
    return_when_done: bool = True
    mode: str = "configured"
    configured_deck: str = ""
    random_decks: list[str] = field(default_factory=list)
    cooldown_seconds: int = 0
    ai_sites: list[str] = field(default_factory=lambda: DEFAULT_AI_SITES.copy())
    ai_apps: list[str] = field(default_factory=lambda: DEFAULT_AI_APPS.copy())
    anki_path: str = ""


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
    config.ai_sites = [str(site).lower() for site in config.ai_sites]
    config.ai_apps = [str(app).lower() for app in config.ai_apps]
    config.cooldown_seconds = max(0, int(config.cooldown_seconds))
    return config


def save_config(config: Config) -> None:
    CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


def choose_deck(config: Config) -> str:
    if config.mode == "random":
        if not config.random_decks:
            raise ValueError("Select at least one deck for random mode.")
        return random.choice(config.random_decks)
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


# --------------------------------------------------------------------------
# Windows integration: start with Windows, desktop shortcut
# --------------------------------------------------------------------------


def launch_target() -> tuple[str, list[str]]:
    """The program and arguments that start Always Learn without a console."""
    if FROZEN:
        return sys.executable, []
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else sys.executable), [str(Path(__file__).resolve())]


def starts_with_windows() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_start_with_windows(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            program, arguments = launch_target()
            command = subprocess.list2cmdline([program, *arguments, STARTUP_FLAG])
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


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


def launch_anki(config: Config) -> None:
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


def start_anki_in_background(config: Config) -> None:
    """Launch Anki at login and minimize it, so later reviews open instantly."""
    if find_anki_window():
        return
    try:
        launch_anki(config)
    except RuntimeError as exc:
        print(f"Always Learn: {exc}", file=sys.stderr)
        return
    deadline = time.monotonic() + ANKI_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        anki_window = find_anki_window()
        if anki_window:
            user32.ShowWindow(anki_window, SW_MINIMIZE)
            return
        time.sleep(0.5)


def press_study_now(anki_window: int) -> None:
    """Press Anki's "Study Now" shortcut (S) on the deck overview."""
    if user32.GetForegroundWindow() != anki_window:
        raise RuntimeError("Windows did not let Anki come to the front, so Study Now was not pressed.")
    user32.keybd_event(VK_S, 0, 0, 0)
    user32.keybd_event(VK_S, 0, KEYEVENTF_KEYUP, 0)


def open_anki_deck(deck_name: str, config: Config) -> None:
    """Open Anki (launching it if needed), focus it and show a card from deck_name."""
    try:
        anki_connect("version", timeout=1)
    except AnkiUnavailable:
        anki_window = find_anki_window()
        if anki_window:
            bring_window_to_front(anki_window)
            raise RuntimeError(
                "Anki is open but AnkiConnect is not responding. "
                "Install the AnkiConnect add-on and restart Anki."
            )
        launch_anki(config)
        if not wait_for_anki_connect(ANKI_STARTUP_TIMEOUT_SECONDS):
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

    def is_generating(self, hwnd: int) -> bool:
        """True while the chat page in hwnd shows a Stop button."""
        if self._uia is None:
            return False
        uia = self._uia_module
        try:
            # Search only the page: the browser's own "Stop loading" button
            # lives in the toolbar.
            document = self._uia.ElementFromHandle(hwnd).FindFirst(
                uia.TreeScope_Descendants, self._document_condition
            )
            if not document:
                return False
            buttons = document.FindAllBuildCache(
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
        self._launch_lock = threading.Lock()
        self._last_trigger = 0.0
        self._hook_thread_id = 0
        self._session: Optional[StudySession] = None

    def start(self) -> None:
        if sys.platform != "win32":
            return
        threading.Thread(target=self._run_keyboard_hook, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._hook_thread_id:
            user32.PostThreadMessageW(self._hook_thread_id, WM_QUIT, 0, 0)

    def _run_keyboard_hook(self) -> None:
        self._hook_thread_id = kernel32.GetCurrentThreadId()
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
                            self._enter_presses.put(user32.GetForegroundWindow())
                    elif message == WM_KEYUP:
                        enter_down = False
            return user32.CallNextHookEx(None, code, message, data)

        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, on_key, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.on_event("error", "Could not watch the Enter key; prompt detection is off.")
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)

    def _run(self) -> None:
        detector = AIWindowDetector()
        own_pid = os.getpid()
        next_generation_scan = 0.0
        while not self.stop_event.is_set():
            try:
                hwnd = self._enter_presses.get(timeout=0.25)
            except queue.Empty:
                hwnd = None
            now = time.monotonic()
            if hwnd:
                _, ai_name = detector.describe(hwnd, self.config)
                if ai_name:
                    self._start_session(hwnd, f"prompt sent in {ai_name}", PROMPT_SEND_DELAY_SECONDS)

            foreground = user32.GetForegroundWindow()
            if foreground and window_pid(foreground) != own_pid:
                description, ai_name = detector.describe(foreground, self.config)
                self.current_window = description
                self.current_is_ai = ai_name is not None
                if ai_name and self._session is None and now >= next_generation_scan:
                    # Catches prompts sent with the mouse instead of Enter.
                    next_generation_scan = now + 0.5
                    if detector.is_generating(foreground):
                        self._start_session(foreground, f"{ai_name} started answering")

            if self._session is not None and now >= self._session.next_check:
                self._update_session(detector, now)

    def _start_session(self, ai_window: int, reason: str, delay: float = 0) -> None:
        if self.trigger(reason, delay):
            self._session = StudySession(ai_window, time.monotonic())

    def _update_session(self, detector: AIWindowDetector, now: float) -> None:
        session = self._session
        session.next_check = now + 0.4
        if not user32.IsWindow(session.ai_window):
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
        if not anki_window or user32.GetForegroundWindow() != anki_window:
            return  # the user already moved on; don't yank windows around
        user32.ShowWindow(anki_window, SW_MINIMIZE)
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
                deck = choose_deck(self.config)
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
        self.width, self.height = int(36 * scale), int(20 * scale)
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
        on = self.variable.get()
        width, height = self.width, self.height
        color = COLORS["blue"] if on else COLORS["border"]
        self.switch.delete("all")
        self.switch.create_oval(0, 0, height, height, fill=color, outline=color)
        self.switch.create_oval(width - height, 0, width, height, fill=color, outline=color)
        self.switch.create_rectangle(height / 2, 0, width - height / 2, height, fill=color, outline=color)
        knob = 3
        left = width - height + knob if on else knob
        self.switch.create_oval(
            left, knob, left + height - 2 * knob, height - knob,
            fill=COLORS["text"], outline=COLORS["text"],
        )


class SegmentedControl(tk.Frame):
    """Side-by-side buttons where exactly one is selected."""

    def __init__(self, parent: tk.Widget, options: list[tuple[str, str]], variable: tk.StringVar) -> None:
        super().__init__(parent, bg=COLORS["surface"], highlightthickness=1,
                         highlightbackground=COLORS["border"], padx=3, pady=3)
        self.variable = variable
        self.buttons: dict[str, tk.Label] = {}
        for column, (value, text) in enumerate(options):
            button = tk.Label(self, text=text, font=FONT_BOLD, padx=14, pady=5, cursor="hand2")
            button.grid(row=0, column=column, sticky="nsew")
            button.bind("<Button-1>", lambda _, value=value: variable.set(value))
            self.columnconfigure(column, weight=1, uniform="segments")
            self.buttons[value] = button
        variable.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        for value, button in self.buttons.items():
            selected = self.variable.get() == value
            button.configure(
                bg=COLORS["blue"] if selected else COLORS["surface"],
                fg=COLORS["on_blue"] if selected else COLORS["muted"],
            )


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
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


def flat_button(
    parent: tk.Widget, text: str, command: Callable[[], None], primary: bool = False
) -> tk.Label:
    background = COLORS["blue"] if primary else COLORS["surface_high"]
    hover = COLORS["blue_hover"] if primary else COLORS["border"]
    button = tk.Label(
        parent, text=text, font=FONT_BOLD, padx=16, pady=7, cursor="hand2",
        bg=background, fg=COLORS["on_blue"] if primary else COLORS["text"],
    )
    button.bind("<Button-1>", lambda _: command())
    button.bind("<Enter>", lambda _: button.configure(bg=hover))
    button.bind("<Leave>", lambda _: button.configure(bg=background))
    return button


class SettingsWindow:
    WIDTH = 440

    def __init__(self, root: tk.Tk, config: Config, monitor: Monitor) -> None:
        self.root = root
        self.config = config
        self.monitor = monitor
        self.tray_icon: Optional[Icon] = None
        self.events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.scale = root.winfo_fpixels("1i") / 96

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
        SegmentedControl(page, [("configured", "One deck"), ("random", "Random")], self.mode).pack(fill="x")
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
        self.start_with_windows = tk.BooleanVar(value=starts_with_windows())
        ToggleRow(
            page, "Start with Windows",
            "Runs in the tray and opens Anki in the background when you log in.",
            self.start_with_windows, self.scale,
        ).pack(fill="x", pady=(self.px(8), 0))
        self.start_with_windows.trace_add(
            "write", lambda *_: set_start_with_windows(self.start_with_windows.get())
        )

        footer = tk.Frame(page, bg=COLORS["background"])
        footer.pack(fill="x", pady=(self.px(16), 0))
        flat_button(footer, "Try it: open Anki now", self.test_now, primary=True).pack(side="left")
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
        self.toggle_button = flat_button(card, "", lambda: self.set_monitoring(not self.config.enabled))
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
        self.random_list = tk.Listbox(
            self.random_picker, selectmode="multiple", exportselection=False,
            height=4, font=FONT, activestyle="none", relief="flat", bd=0,
            bg=COLORS["surface_high"], fg=COLORS["text"],
            selectbackground=COLORS["blue"], selectforeground=COLORS["on_blue"],
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"],
        )
        self.random_list.pack(side="left", fill="x", expand=True)
        self.random_list.bind("<<ListboxSelect>>", lambda _: self.persist())

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

    def _on_mode_change(self) -> None:
        if self.mode.get() == "random":
            self.single_picker.pack_forget()
            self.random_picker.pack(fill="x")
        else:
            self.random_picker.pack_forget()
            self.single_picker.pack(fill="x")
        self.persist()

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
        if not decks:
            decks = sorted(set(self.config.random_decks + [self.config.configured_deck]) - {""})
            self.deck_hint.configure(text="Open Anki to load your decks.", fg=COLORS["yellow"])
        elif self.mode.get() == "random":
            self.deck_hint.configure(text="Click to pick several decks.", fg=COLORS["muted"])
        else:
            self.deck_hint.configure(text=f"{len(decks)} decks in Anki", fg=COLORS["muted"])
        self.deck_combo.configure(values=decks)
        chosen = set(self.config.random_decks)
        self.random_list.delete(0, "end")
        for index, deck in enumerate(decks):
            self.random_list.insert("end", deck)
            if deck in chosen:
                self.random_list.selection_set(index)

    def persist(self) -> None:
        if not hasattr(self, "return_when_done"):
            return  # still constructing
        self.config.return_when_done = self.return_when_done.get()
        self.config.mode = self.mode.get()
        self.config.configured_deck = self.configured_deck.get().strip()
        if self.random_list.size():
            self.config.random_decks = [
                self.random_list.get(index) for index in self.random_list.curselection()
            ]
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
        self.toggle_button.configure(text="Pause" if enabled else "Resume")

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


def main() -> None:
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
    if at_login:
        root.withdraw()
        threading.Thread(target=start_anki_in_background, args=(config,), daemon=True).start()
    monitor.start()
    threading.Thread(target=tray_icon.run, daemon=True).start()
    root.mainloop()
    monitor.stop()
    tray_icon.stop()


if __name__ == "__main__":
    main()
