#!/usr/bin/env python3
"""veyondemo_gui.py - fake service + GUI di controllo.

Il fake veyon-service gira sempre in background su :11100; ogni
StartDemoClient intercettato viene salvato in uno store persistente
(~/.local/share/veyon-demo-viewer/demos.json) cosi' ogni demo resta
accessibile con il suo token. L'auto-apertura lancia demo_viewer.py.

Uso: python3 veyondemo_gui.py [--port 11100] [-v]
"""

import argparse
import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

import veyon_proto as vp
from fake_service import ServiceCtx, run_service

log = logging.getLogger("veyondemo_gui")

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.expanduser(
    "~/.local/share/veyon-demo-viewer/demos.json")


# ------------------------------------------------------------------ store

class DemoStore:
    """Token demo catturati, persistenti su disco (chiave host:port)."""

    def __init__(self, path):
        self.path = path
        self.demos = {}
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                self.demos = json.load(f)
        except Exception:
            self.demos = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.demos, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("store: salvataggio fallito: %s", e)

    @staticmethod
    def key(host, port):
        return f"{host}:{port}"

    def upsert(self, host, port, token, mode):
        rec = self.demos.get(self.key(host, port), {})
        rec.update(host=host, port=port, token=token.hex(), mode=mode,
                   first=rec.get("first", time.time()),
                   last=time.time(), active=True)
        self.demos[self.key(host, port)] = rec
        self.save()
        return rec

    def deactivate(self):
        changed = False
        for rec in self.demos.values():
            if rec.get("active"):
                rec["active"] = False
                changed = True
        if changed:
            self.save()

    def sorted(self):
        return sorted(self.demos.values(),
                      key=lambda r: r.get("last", 0), reverse=True)


# ------------------------------------------------------------------ backend

class Backend(threading.Thread):
    """Loop asyncio con fake service + consumatore eventi demo."""

    def __init__(self, port, bridge, store, verbose):
        super().__init__(daemon=True, name="veyon-backend")
        self.port = port
        self.bridge = bridge            # queue.Queue -> GUI
        self.store = store
        self.verbose = verbose
        self.ctx = None

    async def consume(self, events):
        while True:
            fm, peer = await events.get()
            if fm.uid not in vp.DEMO_UIDS:
                continue
            if fm.command == vp.CMD_START_DEMO_CLIENT:
                host = fm.args.get(vp.ARG_DEMO_SERVER_HOST) or peer
                port = int(fm.args.get(vp.ARG_DEMO_SERVER_PORT) or 11400)
                token = fm.args.get(vp.ARG_DEMO_ACCESS_TOKEN) or b""
                mode = ("fullscreen" if fm.uid == vp.DEMO_UID_FULLSCREEN
                        else "window")
                rec = self.store.upsert(host, port, token, mode)
                self.bridge.put(("demo", rec))
            elif fm.command == vp.CMD_STOP_DEMO_CLIENT:
                was_active = any(r.get("active")
                                 for r in self.store.demos.values())
                self.store.deactivate()
                if was_active:
                    self.bridge.put(("demo_stop", {}))

    def run(self):
        events = asyncio.Queue()
        self.ctx = ServiceCtx(events, verbose=self.verbose)
        self.ctx.bridge = self.bridge

        async def main():
            await asyncio.gather(run_service(self.port, self.ctx),
                                 self.consume(events))
        try:
            asyncio.run(main())
        except Exception as e:
            self.bridge.put(("fatal", {"error": repr(e)}))


class LogBridge(logging.Handler):
    """Inoltra i record di log alla GUI via bridge queue."""

    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.bridge.put(("log", {"line": self.format(record)}))
        except Exception:
            pass


# ------------------------------------------------------------------ GUI

class DemoApp(Adw.Application):
    def __init__(self, args):
        super().__init__(application_id="dev.local.veyon-demo-gui")
        self.args = args
        self.bridge = queue.Queue()
        self.store = DemoStore(STORE_PATH)
        self.viewer_proc = None
        self.rows = {}
        self.backend = None

    # ---- costruzione UI ------------------------------------------------------

    def do_activate(self):
        self.win = Adw.ApplicationWindow(
            application=self, title="Veyon Demo")
        self.win.set_icon_name("veyon-demo-viewer")
        self.win.set_default_size(640, 560)

        header = Adw.HeaderBar()
        self.status_dot = Gtk.Image(icon_name="media-record-symbolic")
        self.status_dot.set_pixel_size(10)
        header.pack_start(self.status_dot)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(header)

        clamp = Adw.Clamp(maximum_size=560)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        clamp.set_child(content)
        outer.append(clamp)
        self.win.set_content(outer)

        # -- gruppo stato --
        grp = Adw.PreferencesGroup(title="Servizio")

        self.svc_row = Adw.ActionRow(
            title="Fake service",
            subtitle=f"in ascolto sulla porta {self.args.port}")
        self.svc_row.set_icon_name("network-server-symbolic")
        self.svc_lbl = Gtk.Label(label="avvio…")
        self.svc_lbl.add_css_class("dim-label")
        self.svc_lbl.set_valign(Gtk.Align.CENTER)
        self.svc_row.add_suffix(self.svc_lbl)
        grp.add(self.svc_row)

        self.masters_row = Adw.ActionRow(
            title="Master connessi", subtitle="nessuno")
        self.masters_row.set_icon_name("system-users-symbolic")
        grp.add(self.masters_row)
        content.append(grp)

        # -- gruppo demo --
        self.demo_grp = Adw.PreferencesGroup(title="Presentazioni")

        self.auto_row = Adw.ActionRow(
            title="Apertura automatica",
            subtitle="apre il viewer a ogni nuova presentazione")
        self.auto_row.set_icon_name("media-playback-start-symbolic")
        self.auto_sw = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.auto_row.add_suffix(self.auto_sw)
        self.auto_row.set_activatable_widget(self.auto_sw)
        self.demo_grp.add(self.auto_row)

        self.empty_row = Adw.ActionRow(
            title="Nessuna presentazione intercettata",
            subtitle="in attesa di StartDemoClient dal master")
        self.empty_row.set_icon_name("video-display-symbolic")
        self.empty_row.set_sensitive(False)
        self.demo_grp.add(self.empty_row)

        content.append(self.demo_grp)

        # -- gruppo log --
        self.log_grp = Adw.ExpanderRow(title="Registro eventi")
        self.log_grp.set_icon_name("text-x-generic-symbolic")
        self.log_grp.set_show_enable_switch(False)

        scrolled = Gtk.ScrolledWindow(min_content_height=150,
                                      max_content_height=220,
                                      propagate_natural_height=True)
        self.logview = Gtk.TextView(editable=False, monospace=True,
                                    top_margin=6, bottom_margin=6,
                                    left_margin=8, right_margin=8)
        self.logview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scrolled.set_child(self.logview)
        frame = Gtk.Frame(child=scrolled)
        frame.set_margin_top(4)
        frame.set_margin_bottom(8)
        self.log_grp.add_row(frame)
        content.append(self.log_grp)

        self.win.connect("close-request", self._on_close)
        self.win.present()

        # backend asyncio in thread
        self.backend = Backend(self.args.port, self.bridge, self.store,
                               self.args.verbose)
        self.backend.start()

        handler = LogBridge(self.bridge)
        handler.setLevel(logging.INFO if not self.args.verbose
                         else logging.DEBUG)
        logging.getLogger().addHandler(handler)

        GLib.timeout_add(150, self._poll_bridge)
        for rec in self.store.sorted():
            self.add_row(rec)
        self.log_line(f"in ascolto su :{self.args.port} — "
                      "attendo il veyon-master")

    # ---- helpers UI ---------------------------------------------------------

    def log_line(self, text):
        buf = self.logview.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.logview.scroll_to_mark(mark, 0.0, False, 0.0, 1.0)

    def add_row(self, rec):
        key = DemoStore.key(rec["host"], rec["port"])
        old = self.rows.pop(key, None)
        if old is not None:
            self.demo_grp.remove(old)
        self.empty_row.set_visible(False)

        mode = "schermo intero" if rec["mode"] == "fullscreen" else "finestra"
        ts = time.strftime("%H:%M", time.localtime(rec["last"]))
        active = rec.get("active")

        row = Adw.ActionRow(title=f"{rec['host']}:{rec['port']}")
        row.set_icon_name("video-display-symbolic")
        row.set_subtitle(
            f"{mode} · token delle {ts} · "
            f"{'attiva' if active else 'terminata'}")

        if active:
            pill = Gtk.Label(label="LIVE")
            pill.add_css_class("accent")
            pill.add_css_class("caption-heading")
            pill.set_valign(Gtk.Align.CENTER)
            row.add_suffix(pill)

        btn = Gtk.Button(label="Apri", valign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action" if active else "flat")
        btn.connect("clicked", lambda *_: self.open_viewer(rec))
        row.add_suffix(btn)

        self.demo_grp.add(row)
        self.rows[key] = row

    def open_viewer(self, rec):
        self.kill_viewer()
        self.log_line(f"apertura viewer su {rec['host']}:{rec['port']}…")
        self.viewer_proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "demo_viewer.py"),
             rec["host"], str(rec["port"]), rec["token"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def kill_viewer(self):
        if self.viewer_proc and self.viewer_proc.poll() is None:
            self.viewer_proc.terminate()
        self.viewer_proc = None

    # ---- bridge poll ---------------------------------------------------------

    def _poll_bridge(self):
        try:
            while True:
                kind, payload = self.bridge.get_nowait()
                self._on_bridge(kind, payload)
        except queue.Empty:
            pass
        return True

    def _on_bridge(self, kind, payload):
        if kind == "log":
            line = payload["line"]
            if " in ascolto su :" in line:
                self.svc_lbl.set_text("attivo")
                self.svc_lbl.remove_css_class("dim-label")
                self.svc_lbl.add_css_class("success")
                self.status_dot.add_css_class("success")
            if "FeatureMessage" not in line or self.args.verbose:
                self.log_line(line)
        elif kind == "conn":
            self.log_line(
                f"master online: {payload['user'] or '?'} ({payload['ip']})")
            self._refresh_masters()
        elif kind == "disc":
            self.log_line(f"master disconnesso: {payload['ip']}")
            self._refresh_masters()
        elif kind == "demo":
            rec = payload
            self.add_row(rec)
            self.log_line(
                f"nuova demo su {rec['host']}:{rec['port']} "
                f"({rec['mode']}) — token catturato")
            if self.auto_sw.get_active():
                self.open_viewer(rec)
        elif kind == "demo_stop":
            self.log_line("presentazione terminata — chiudo il viewer")
            self.kill_viewer()
            for rec in self.store.sorted():
                self.add_row(rec)
        elif kind == "fatal":
            self.svc_lbl.set_text("errore")
            self.svc_lbl.add_css_class("error")
            self.log_line(f"FATALE: {payload['error']}")

    def _refresh_masters(self):
        ctx = self.backend.ctx if self.backend else None
        masters = ctx.masters if ctx else {}
        txt = ("; ".join(f"{u or '?'} ({ip})" for ip, u in masters.items())
               if masters else "nessuno")
        self.masters_row.set_subtitle(txt)

    def _on_close(self, *_):
        self.kill_viewer()
        return False


def _install_icon():
    dst = os.path.expanduser("~/.local/share/icons/hicolor/scalable/apps")
    try:
        os.makedirs(dst, exist_ok=True)
        import shutil
        shutil.copyfile(os.path.join(HERE, "assets", "icon.svg"),
                        os.path.join(dst, "veyon-demo-viewer.svg"))
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        level=logging.DEBUG if args.verbose else logging.INFO)

    Adw.init()
    _install_icon()
    app = DemoApp(args)
    app.run(sys.argv[:1])


if __name__ == "__main__":
    main()
