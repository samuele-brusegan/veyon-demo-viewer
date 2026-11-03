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
        return fd_list.get(fd_idx), node

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

    def _run(self):
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
            self.frame = None
