"""Opens YouTube Music in the installed desktop app (a Brave/Chrome/Edge "installed app" / PWA) on Windows,
optionally in the background: the window is minimized at once and the focus goes back to what you were using.

Used by play_music and by system_control open_url for music.youtube.com links. Falls back to the browser
when no app is installed. No SKILLS here: it is a helper module.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

from ..config import settings

log = logging.getLogger("jarvis.ytm_app")
_app_cache: dict | None = None
_app_searched = False

FIND_SHORTCUT_PS = r"""
$sh = New-Object -ComObject WScript.Shell
$dirs = @([Environment]::GetFolderPath('Desktop'), "$env:PUBLIC\Desktop",
          "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")
foreach ($d in $dirs) {
  Get-ChildItem $d -Recurse -Depth 3 -Filter *.lnk -ErrorAction SilentlyContinue |
    Where-Object { $_.BaseName -like '*YouTube Music*' } | ForEach-Object {
      $l = $sh.CreateShortcut($_.FullName)
      if ($l.Arguments -match '--app-id=') { "$($l.TargetPath)|$($l.Arguments)"; exit }
    }
}
"""


def _shortcut_from_lnk(path: str) -> dict | None:
    ps = (f"$l=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}'); "
          "\"$($l.TargetPath)|$($l.Arguments)\"")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, timeout=15).stdout.strip()
    return _parse(out)


def _parse(line: str) -> dict | None:
    if "|" not in line:
        return None
    target, args = line.split("|", 1)
    if not target or "--app-id=" not in args or not Path(target).exists():
        return None
    return {"target": target, "args": args.strip()}


def find_app() -> dict | None:
    """Locate the YouTube Music desktop app shortcut (cached). Windows only."""
    global _app_cache, _app_searched
    if _app_searched:
        return _app_cache
    _app_searched = True
    if sys.platform != "win32":
        return None
    try:
        custom = settings.ytmusic_shortcut
        if custom:
            # only a real .lnk file path, so it is safe inside the PowerShell single quotes
            if custom.lower().endswith(".lnk") and Path(custom).is_file() and "'" not in custom:
                _app_cache = _shortcut_from_lnk(custom)
        else:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", FIND_SHORTCUT_PS],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            _app_cache = _parse(out.splitlines()[0]) if out else None
    except Exception as exc:
        log.warning("YouTube Music app lookup failed: %s", exc)
        _app_cache = None
    log.info("YouTube Music desktop app: %s", _app_cache["target"] if _app_cache else "not found (browser will be used)")
    return _app_cache


# ---------------- Windows window helpers ----------------
def _windows_titled(fragment: str) -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value
                # app windows are titled "... YouTube Music"; browser tabs end with "- Brave"/"- Google Chrome"...
                if fragment in title and not title.rstrip().endswith(("Brave", "Chrome", "Edge")):
                    found.append((hwnd, title))
        return True

    user32.EnumWindows(callback, 0)
    return found


def _launch_sync(url: str, foreground: bool) -> dict:
    import ctypes

    user32 = ctypes.windll.user32
    app = find_app()
    if not app:
        return {"ok": False, "error": "YouTube Music app not found"}
    previous = user32.GetForegroundWindow()
    # Close the player already open, otherwise two songs would play at the same time
    for hwnd, _title in _windows_titled("YouTube Music"):
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(0.4)
    cmd = f'"{app["target"]}" {app["args"]} --app-launch-url-for-shortcuts-menu-item="{url}"'
    si = subprocess.STARTUPINFO()
    if not foreground:
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE: start minimized without taking the focus
    subprocess.Popen(cmd, startupinfo=si, close_fds=True)
    if foreground:
        return {"ok": True, "opened_in": "YouTube Music app", "window": "in front"}
    # Browsers often ignore the "minimized" hint: as soon as the window appears, minimize it and give the focus back
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.25)
        wins = _windows_titled("YouTube Music")
        if wins:
            for hwnd, _title in wins:
                user32.ShowWindow(hwnd, 7)            # SW_SHOWMINNOACTIVE
            if previous:
                user32.keybd_event(0x12, 0, 0, 0)     # tap Alt: Windows then allows giving the focus back
                user32.keybd_event(0x12, 0, 2, 0)
                user32.SetForegroundWindow(previous)
            return {"ok": True, "opened_in": "YouTube Music app", "window": "minimized in the background"}
    return {"ok": True, "opened_in": "YouTube Music app", "window": "unknown (window not detected)"}


async def open_in_app(url: str, foreground: bool | None = None) -> dict | None:
    """Open a music.youtube.com URL in the desktop app. Returns None when the app can't be used."""
    if settings.music_player != "app" or sys.platform != "win32":
        return None
    if not await asyncio.to_thread(find_app):
        return None
    fg = settings.music_foreground if foreground is None else foreground
    return await asyncio.to_thread(_launch_sync, url, fg)
