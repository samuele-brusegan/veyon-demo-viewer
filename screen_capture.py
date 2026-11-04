"""screen_capture.py - cattura schermo via XDG ScreenCast portal + PipeWire.

Su GNOME Wayland non esiste accesso diretto al framebuffer: il percorso
corretto e' org.freedesktop.portal.ScreenCast -> PipeWire -> GStreamer
(pipewiresrc). Alla prima attivazione GNOME mostra il dialogo di
consenso; con persist_mode=2 il consenso viene ricordato e salviamo il
restore_token per non richiederlo agli avvii successivi.

Uso:
    cap = ScreenCapture(on_status=print)
    cap.start()          # asincrono: il consenso puo' richiedere un click
    fb = cap.get_frame() # np (h,w,3) uint8 oppure None
    cap.stop()
"""

import logging
import os
import secrets
import threading
import time

import numpy as np

log = logging.getLogger("screen_capture")

RESTORE_TOKEN_PATH = os.path.expanduser(
    "~/.local/share/veyon-demo-viewer/portal-token")

_PORTAL = "org.freedesktop.portal.Desktop"
_PORTAL_OBJ = "/org/freedesktop/portal/desktop"
_SCIFACE = "org.freedesktop.portal.ScreenCast"


def _load_restore_token():
    try:
        with open(RESTORE_TOKEN_PATH) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_restore_token(tok):
    try:
        os.makedirs(os.path.dirname(RESTORE_TOKEN_PATH), exist_ok=True)
        with open(RESTORE_TOKEN_PATH, "w") as f:
            f.write(tok)
    except OSError:
        pass


class ScreenCapture:
    """Thread che gira il dialogo portal e la pipeline Gst."""

    def __init__(self, on_status=None):
        self.on_status = on_status or (lambda t: log.info("%s", t))
        self.frame = None
        self._stop = threading.Event()
        self._thread = None
        self._mctx = None
        self._pipe = None
        self._conn = None          # connessione D-Bus di sessione
        self._session = None       # object path portal Session
        self._pwfd = None          # fd PipeWire remoto

    # -- API ------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="screen-capture")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._mctx:
            try:
                self._mctx.wakeup()       # sblocca iteration(True) in _run
            except Exception:
                pass
        t = self._thread
        # join: la chiusura della sessione portal (indicatore GNOME)
        # avviene nel finally di _run — aspetto che completi
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)

    def get_frame(self):
        return self.frame

    # -- D-Bus helper ------------------------------------------------------

    def _portal_request(self, conn, iface_method, params):
        """Invoca un metodo portal e attende il segnale Request/Response.

        Il path del Request object non e' predicibile (qui e'
        /org/freedesktop/portal/desktop/request/...), quindi filtro sul
        token univoco nel path invece di sottoscrivere un path fisso."""
        from gi.repository import Gio, GLib
        token = "veyon" + secrets.token_hex(6)
        out = {}
        done = threading.Event()

        def on_resp(_c, _s, path, _i, _sig, args):
            if token in path:
                resp, results = args.unpack()
                out["resp"] = resp
                out["results"] = results
                done.set()

        params["handle_token"] = GLib.Variant("s", token)
        sub = conn.signal_subscribe(
            _PORTAL, "org.freedesktop.portal.Request", "Response",
            None, None, Gio.DBusSignalFlags.NONE, on_resp)
        try:
            iface_method(params)
            # i segnali arrivano sul thread-default context di questo
            # thread (pushato in _run): lo itero io cosi' funziona sia
            # nella GUI che nel daemon, dove non gira alcun main loop.
            ctx = GLib.MainContext.get_thread_default()
            deadline = time.monotonic() + 25
            while not done.wait(0.05):
                while ctx.iteration(False):
                    pass
                if self._stop.is_set():
                    raise RuntimeError("cattura annullata")
                if time.monotonic() > deadline:
                    raise TimeoutError("portal: timeout attesa risposta")
        finally:
            conn.signal_unsubscribe(sub)
        if out.get("resp") != 0:
            raise RuntimeError(f"portal: richiesta rifiutata "
                               f"(resp={out.get('resp')})")
        return out["results"]

    def _portal_open(self):
        """Sessione ScreenCast completa: ritorna (fd_pipewire, node_id)."""
        from gi.repository import Gio, GLib
        conn = Gio.bus_get_sync(Gio.BusType.SESSION)
        self._conn = conn

        def call(method, iface, params, sig):
            conn.call_sync(_PORTAL, _PORTAL_OBJ, iface,
                           method, GLib.Variant(sig, params),
                           None, Gio.DBusCallFlags.NONE, 15000, None)

        sess_tok = "sess" + secrets.token_hex(6)
        res = self._portal_request(
            conn,
            lambda p: call("CreateSession", _SCIFACE, (p,), "(a{sv})"),
            {"session_handle_token": GLib.Variant("s", sess_tok)})
        session = str(res["session_handle"])
        self._session = session

        opts = {"types": GLib.Variant("u", 1),          # monitor
                "multiple": GLib.Variant("b", False),
                "persist_mode": GLib.Variant("u", 2)}
        rt = _load_restore_token()
        if rt:
            opts["restore_token"] = GLib.Variant("s", rt)
        self._portal_request(
            conn,
            lambda p: call("SelectSources", _SCIFACE,
                           (session, p), "(oa{sv})"),
            opts)

        res = self._portal_request(
            conn,
            lambda p: call("Start", _SCIFACE,
                           (session, "", p), "(osa{sv})"),
            {})
        streams = res.get("streams") or []
        if not streams:
            raise RuntimeError("portal: nessuno stream concesso")
        node = int(streams[0][0])
        rt2 = res.get("restore_token")
        if rt2:
            _save_restore_token(str(rt2))

        # OpenPipeWireRemote -> fd
        result, fd_list = conn.call_with_unix_fd_list_sync(
            _PORTAL, _PORTAL_OBJ, _SCIFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})),
            GLib.VariantType.new("(h)"),
            Gio.DBusCallFlags.NONE, 15000, None, None)
        (fd_idx,) = result.unpack()
        self._pwfd = fd_list.get(fd_idx)
        return self._pwfd, node

    # -- GStreamer ---------------------------------------------------------

    def _on_sample(self, sink):
        from gi.repository import Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        try:
            caps = sample.get_caps().get_structure(0)
            w, h = caps.get_value("width"), caps.get_value("height")
            buf = sample.get_buffer()
            ok, mi = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.ERROR
            try:
                arr = np.frombuffer(mi.data, np.uint8)
                self.frame = arr[: h * w * 3].reshape(h, w, 3).copy()
            finally:
                buf.unmap(mi)
        except Exception as e:
            log.warning("sample: %s", e)
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    # ---- fallback X11 (XFCE/LXDE/ecc. — niente portale necessario) ----------

    def _run_x11(self):
        """Cattura via XGetImage sul root window (~3 fps).

        Su X11 qualsiasi client della sessione puo' leggere il root
        window: niente portale, niente consenso. Usato su sessioni
        X11 (XFCE, LXDE...) dove il portale ScreenCast non esiste."""
        import ctypes
        import ctypes.util

        class XImage(ctypes.Structure):
            _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                        ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                        ("data", ctypes.c_void_p),
                        ("byte_order", ctypes.c_int),
                        ("bitmap_unit", ctypes.c_int),
                        ("bitmap_bit_order", ctypes.c_int),
                        ("bitmap_pad", ctypes.c_int),
                        ("depth", ctypes.c_int),
                        ("bytes_per_line", ctypes.c_int),
                        ("bits_per_pixel", ctypes.c_int),
                        ("red_mask", ctypes.c_ulong),
                        ("green_mask", ctypes.c_ulong),
                        ("blue_mask", ctypes.c_ulong)]

        class XWindowAttributes(ctypes.Structure):
            _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                        ("width", ctypes.c_int), ("height", ctypes.c_int),
                        ("border_width", ctypes.c_int),
                        ("depth", ctypes.c_int),
                        ("visual", ctypes.c_void_p),
                        ("root", ctypes.c_ulong),
                        ("class_", ctypes.c_int),
                        ("bit_gravity", ctypes.c_int),
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

        xlib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint, ctypes.c_uint,
                                   ctypes.c_ulong, ctypes.c_int]
        xlib.XGetImage.restype = ctypes.POINTER(XImage)
        xlib.XFree.argtypes = [ctypes.c_void_p]
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xlib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        xlib.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(XWindowAttributes)]
        xlib.XGetWindowAttributes.restype = ctypes.c_int

        # gli errori X di default uccidono il processo: li trasformo in
        # eccezioni gestibili (es. XGetImage su root di XWayland)
        xerr = []
        ERRHANDLER = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def _on_xerr(_d, ev):
            xerr.append(ev)
            return 0
        handler = ERRHANDLER(_on_xerr)
        xlib.XSetErrorHandler.argtypes = [ERRHANDLER]
        xlib.XSetErrorHandler(handler)

        dpy = xlib.XOpenDisplay(None)
        if not dpy:
            raise RuntimeError("X11: impossibile aprire DISPLAY")
        try:
            root = xlib.XDefaultRootWindow(dpy)
            attr = XWindowAttributes()
            xlib.XGetWindowAttributes(dpy, root, ctypes.byref(attr))
            W, H = attr.width, attr.height
            self.on_status(f"cattura X11 attiva ({W}x{H}) — nessun "
                           "consenso necessario")
            while not self._stop.is_set():
                xerr.clear()
                img = xlib.XGetImage(dpy, root, 0, 0, W, H,
                                     0xFFFFFFFF, 2)      # ZPixmap
                xlib.XSync(dpy, 0)                       # forza errori async
                if xerr or not img:
                    raise RuntimeError(
                        "X11: XGetImage fallito (root non catturabile)")
                try:
                    im = img.contents
                    bpp = im.bits_per_pixel // 8
                    raw = ctypes.string_at(im.data,
                                           im.bytes_per_line * im.height)
                    arr = np.frombuffer(raw, np.uint8).reshape(
                        im.height, im.bytes_per_line)
                    px = arr[:, : im.width * bpp].reshape(
                        im.height, im.width, bpp)
                    # LSBFirst: B,G,R(,X); MSBFirst 32bpp: X,R,G,B;
                    # MSBFirst 24bpp: R,G,B
                    if im.byte_order == 1:
                        ch = [1, 2, 3] if bpp >= 4 else [0, 1, 2]
                    else:
                        ch = [2, 1, 0]
                    self.frame = px[:, :, ch][: ,: , :3].copy()
                finally:
                    xlib.XFree(im.data)
                    xlib.XFree(img)
                self._stop.wait(0.33)                    # ~3 fps
        finally:
            xlib.XCloseDisplay(dpy)

    def _run(self):
        # su sessione X11 (XFCE ecc.) il portale ScreenCast tipicamente
        # non esiste: si va diretti su XGetImage, senza consenso
        if (os.environ.get("XDG_SESSION_TYPE") == "x11"
                and os.environ.get("DISPLAY")):
            try:
                self._run_x11()
            except Exception as e:
                self.on_status(f"cattura schermo non disponibile: {e}")
                log.exception("screen capture X11 fallita")
            self.frame = None
            return
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst, GLib
            Gst.init(None)
            # context dedicato: i callback D-Bus portal vengono
            # dispatchati qui, iterato dal thread stesso (niente
            # dipendenza da un main loop GTK esterno)
            self._mctx = GLib.MainContext.new()
            self._mctx.push_thread_default()
            fd, node = self._portal_open()
            self.on_status("consenso schermo ottenuto — cattura avviata")
            desc = (f"pipewiresrc fd={fd} path={node} do-timestamp=false ! "
                    "videoconvert ! video/x-raw,format=RGB ! "
                    "appsink name=s emit-signals=true "
                    "max-buffers=1 drop=true")
            self._pipe = Gst.parse_launch(desc)
            sink = self._pipe.get_by_name("s")
            sink.connect("new-sample", self._on_sample)
            self._pipe.set_state(Gst.State.PLAYING)
            while not self._stop.is_set():
                self._mctx.iteration(True)
        except Exception as e:
            self.on_status(f"cattura schermo non disponibile: {e}")
            log.exception("screen capture fallita")
        finally:
            if self._pipe:
                try:
                    from gi.repository import Gst
                    self._pipe.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self._pipe = None
            # chiusura esplicita della sessione portal: senza questa
            # GNOME continua a mostrare l'indicatore "registrazione
            # schermo" nella barra superiore
            if self._pwfd is not None:
                try:
                    os.close(self._pwfd)
                except OSError:
                    pass
                self._pwfd = None
            if self._session and self._conn:
                try:
                    from gi.repository import Gio, GLib
                    self._conn.call_sync(
                        _PORTAL, self._session,
                        "org.freedesktop.portal.Session", "Close",
                        None, None, Gio.DBusCallFlags.NONE, 5000, None)
                except Exception as e:
                    log.debug("chiusura sessione portal: %s", e)
            self._session = None
            self._conn = None
            self.frame = None
            if self._mctx:
                try:
                    self._mctx.pop_thread_default()
                except Exception:
                    pass
                self._mctx = None
