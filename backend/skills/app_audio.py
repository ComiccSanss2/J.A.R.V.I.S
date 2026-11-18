"""Per-application volume (the Windows Volume Mixer), via Core Audio COM interfaces called with ctypes.

"Turn YouTube Music down" moves that app's slider in the Windows volume mixer, not the master volume.
Note: browsers play all their audio from one process, so the YouTube Music app (a Brave/Chrome/Edge app)
shares its mixer slider with that browser's tabs. No SKILLS here: helper used by media.py.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import HRESULT, POINTER, byref, c_float, c_int, c_uint, c_void_p, wintypes

import psutil


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(text, byref(self))


CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
IID_ISimpleAudioVolume = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"   # the mixer slider + mute
IID_IChannelAudioVolume = "{1C158861-B533-4B30-B1CF-E853E51C59B8}"  # per-channel levels (kept at 100%)
CLSCTX_ALL = 23
eRender, eMultimedia = 0, 1


def _call(obj: c_void_p, index: int, restype, *args_types):
    """Return a callable for vtable method `index` of COM object `obj`."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *args_types)
    fn = proto(vtbl[index])
    return lambda *a: fn(obj, *a)


def _release(obj: c_void_p) -> None:
    if obj:
        _call(obj, 2, c_uint)()


def _qi(obj: c_void_p, iid: str) -> c_void_p:
    out = c_void_p()
    _call(obj, 0, HRESULT, POINTER(GUID), POINTER(c_void_p))(byref(GUID(iid)), byref(out))
    return out


class Session:
    def __init__(self, name: str, pid: int, simple: c_void_p, channels: c_void_p):
        self.name, self.pid, self.simple, self.channels = name, pid, simple, channels

    # ---- mixer slider (ISimpleAudioVolume) ----
    def volume(self) -> float:
        level = c_float()
        _call(self.simple, 4, HRESULT, POINTER(c_float))(byref(level))
        return level.value

    def set_volume(self, level: float) -> None:
        _call(self.simple, 3, HRESULT, c_float, c_void_p)(max(0.0, min(1.0, level)), None)

    def muted(self) -> bool:
        m = wintypes.BOOL()
        _call(self.simple, 6, HRESULT, POINTER(wintypes.BOOL))(byref(m))
        return bool(m.value)

    def set_mute(self, on: bool) -> None:
        _call(self.simple, 5, HRESULT, wintypes.BOOL, c_void_p)(on, None)

    # ---- channel levels (IChannelAudioVolume): reset to 100% so the slider alone sets the volume ----
    def channel_level(self) -> float:
        n = c_uint()
        _call(self.channels, 3, HRESULT, POINTER(c_uint))(byref(n))
        if not n.value:
            return 1.0
        levels = (c_float * n.value)()
        _call(self.channels, 7, HRESULT, c_uint, POINTER(c_float))(n.value, levels)
        return sum(levels) / n.value

    def reset_channels(self) -> None:
        n = c_uint()
        _call(self.channels, 3, HRESULT, POINTER(c_uint))(byref(n))
        if n.value:
            levels = (c_float * n.value)(*([1.0] * n.value))
            _call(self.channels, 6, HRESULT, c_uint, POINTER(c_float), c_void_p)(n.value, levels, None)


def _sessions():
    """Yield a Session for every audio session on the default speakers."""
    ole32 = ctypes.oledll.ole32
    ole32.CoInitializeEx(None, 2)  # each call runs in its own worker thread
    enum = c_void_p()
    ole32.CoCreateInstance(byref(GUID(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                           byref(GUID(IID_IMMDeviceEnumerator)), byref(enum))
    device, manager, sessions = c_void_p(), c_void_p(), c_void_p()
    try:
        _call(enum, 4, HRESULT, c_int, c_int, POINTER(c_void_p))(eRender, eMultimedia, byref(device))
        _call(device, 3, HRESULT, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))(
            byref(GUID(IID_IAudioSessionManager2)), CLSCTX_ALL, None, byref(manager))
        _call(manager, 5, HRESULT, POINTER(c_void_p))(byref(sessions))
        count = c_int()
        _call(sessions, 3, HRESULT, POINTER(c_int))(byref(count))
        for i in range(count.value):
            ctl = c_void_p()
            _call(sessions, 4, HRESULT, c_int, POINTER(c_void_p))(i, byref(ctl))
            ctl2 = _qi(ctl, IID_IAudioSessionControl2)
            pid = wintypes.DWORD()
            _call(ctl2, 14, HRESULT, POINTER(wintypes.DWORD))(byref(pid))
            try:
                name = psutil.Process(pid.value).name().lower() if pid.value else "system"
            except psutil.Error:
                name = "?"
            simple = _qi(ctl, IID_ISimpleAudioVolume)
            channels = _qi(ctl, IID_IChannelAudioVolume)
            try:
                yield Session(name, pid.value, simple, channels)
            finally:
                for obj in (channels, simple, ctl2, ctl):
                    _release(obj)
    finally:
        for obj in (sessions, manager, device, enum):
            _release(obj)
        ole32.CoUninitialize()


def list_sessions() -> list[dict]:
    if sys.platform != "win32":
        return []
    return [{"process": s.name, "pid": s.pid, "volume_pct": round(s.volume() * 100), "muted": s.muted()}
            for s in _sessions()]


def change_volume(process_names: set[str], action: str, amount_pct: int = 10) -> dict:
    """action: up | down | set | mute | unmute. Applies to every session of the given processes."""
    if sys.platform != "win32":
        return {"error": "Per-app volume is only available on Windows."}
    changed = []
    for s in _sessions():
        if s.name not in process_names:
            continue
        # what you actually hear = slider x channel level; fold it all into the slider
        current = s.volume() * s.channel_level()
        s.reset_channels()
        if action == "up":
            new = current + amount_pct / 100
        elif action == "down":
            new = current - amount_pct / 100
        elif action == "set":
            new = amount_pct / 100
        elif action in ("mute", "unmute"):
            new = current
        else:
            return {"error": f"Unknown action {action}"}
        new = max(0.0, min(1.0, new))
        s.set_volume(new)
        if action in ("up", "set", "unmute"):
            s.set_mute(False)
        elif action == "mute":
            s.set_mute(True)
        changed.append({"process": s.name, "from_pct": round(current * 100), "to_pct": round(new * 100),
                        "muted": action == "mute"})
    if not changed:
        return {"error": "That app is not playing any sound right now (it has no entry in the Windows volume mixer).",
                "apps_playing": sorted({x["process"] for x in list_sessions()})}
    return {"ok": True, "action": action, "changed": changed}
