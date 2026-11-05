"""recorder.py - registrazione video su file .mkv via ffmpeg.

Due sorgenti:

- PipeRecorder: frame RGB (np h,w,3) scritti in uno stdin ffmpeg
  rawvideo -> H.264. Usato quando i frame vengono da ScreenCapture
  (portale Wayland) o da qualsiasi produttore np.
- X11GrabRecorder: `ffmpeg -f x11grab` nativo — schermo intero oppure
  una singola finestra (-window_id). Solo sessioni X11.

I file vanno nella cartella Video dell'utente.
"""

import logging
import os
import queue
import shutil
import subprocess
import threading
import time

log = logging.getLogger("recorder")


def ffmpeg_ok():
    return shutil.which("ffmpeg") is not None


def is_x11():
    return (os.environ.get("XDG_SESSION_TYPE") == "x11"
            and bool(os.environ.get("DISPLAY")))


def videos_dir():
    try:
        from gi.repository import GLib
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
        if d:
            return d
    except Exception:
        pass
    return os.path.expanduser("~")


def new_path(prefix="veyon-rec"):
    d = videos_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.expanduser("~")
    return os.path.join(
        d, f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.mkv")


_ENC = ["-an", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-pix_fmt", "yuv420p"]


def window_xid(win):
    """XID di una Gtk.Window su sessione X11 (None su Wayland)."""
    try:
        surf = win.get_surface()
        if surf is None:
            return None
        from gi.repository import GdkX11
        if isinstance(surf, GdkX11.X11Surface):
            return int(surf.get_xid())
    except Exception:
        pass
    return None


class PipeRecorder:
    """Frame RGB24 -> ffmpeg rawvideo -> mkv."""

    def __init__(self, path, w, h, fps=10):
        self.path = path
        self.w, self.h, self.fps = int(w), int(h), int(fps)
        self.proc = None
        self._q = queue.Queue(maxsize=self.fps * 4)
        self._wr = None

    def start(self):
        self.proc = subprocess.Popen(
            ["ffmpeg", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{self.w}x{self.h}", "-r", str(self.fps),
             "-i", "pipe:0"] + _ENC + [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wr = threading.Thread(target=self._writer, daemon=True,
                                    name="rec-writer")
        self._wr.start()

    def push(self, frame):
        """frame: np (h,w,3) uint8 RGB. Frame in eccesso vengono
        scartati se ffmpeg non sta dietro."""
        if self.proc is None or frame is None:
            return
        if frame.shape != (self.h, self.w, 3):
            return
        try:
            self._q.put_nowait(frame.tobytes())
        except queue.Full:
            pass

    def _writer(self):
        while True:
            data = self._q.get()
            if data is None:
                break
            try:
                self.proc.stdin.write(data)
            except (BrokenPipeError, ValueError, OSError):
                break

    def stop(self):
        if self.proc is None:
            return
        try:
            self._q.put(None, timeout=2)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass
        if self._wr:
            self._wr.join(timeout=3)
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        self.proc = None
        log.info("registrazione salvata: %s", self.path)


class X11GrabRecorder:
    """`ffmpeg -f x11grab`: schermo intero o una finestra X (window_id).

    Solo X11: su Wayland XWayland non espone le finestre native.
    """

    def __init__(self, path, window_id=None, size=None, fps=15):
        self.path = path
        self.window_id = window_id
        self.size = size              # (w,h) per lo schermo intero
        self.fps = int(fps)
        self.proc = None

    def start(self):
        dpy = os.environ.get("DISPLAY", ":0")
        args = ["ffmpeg", "-y", "-f", "x11grab",
                "-framerate", str(self.fps)]
        if self.window_id is not None:
            args += ["-window_id", hex(int(self.window_id))]
        elif self.size:
            args += ["-video_size", f"{self.size[0]}x{self.size[1]}"]
        # senza -video_size x11grab cattura l'intero display
        args += ["-i", dpy] + _ENC + [self.path]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc is None:
            return
        try:                    # 'q' = stop pulito: finalize il mkv
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        log.info("registrazione salvata: %s", self.path)


def x11_screen_size():
    """Geometria del root window via ctypes (None se non ricavabile)."""
    import ctypes
    import ctypes.util

    class Attr(ctypes.Structure):
        _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                    ("width", ctypes.c_int), ("height", ctypes.c_int),
                    ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
                    ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong),
                    ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                    ("win_gravity", ctypes.c_int),
                    ("backing_store", ctypes.c_int),
                    ("backing_planes", ctypes.c_ulong),
                    ("backing_pixel", ctypes.c_ulong),
                    ("save_under", ctypes.c_int),
                    ("colormap", ctypes.c_ulong),
                    ("map_installed", ctypes.c_int),
                    ("map_state", ctypes.c_int),
                    ("all_event_masks", ctypes.c_long),
                    ("your_event_mask", ctypes.c_long),
                    ("do_not_propagate_mask", ctypes.c_long),
                    ("override_redirect", ctypes.c_int),
                    ("screen", ctypes.c_void_p)]
    try:
        xlib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Attr)]
        xlib.XGetWindowAttributes.restype = ctypes.c_int
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        dpy = xlib.XOpenDisplay(None)
        if not dpy:
            return None
        try:
            a = Attr()
            xlib.XGetWindowAttributes(
                dpy, xlib.XDefaultRootWindow(dpy), ctypes.byref(a))
            return (a.width, a.height)
        finally:
            xlib.XCloseDisplay(dpy)
    except Exception:
        return None
