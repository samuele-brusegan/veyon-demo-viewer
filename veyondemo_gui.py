#!/usr/bin/env python3
"""veyondemo_gui.py - fake service + GUI di controllo.

Il fake veyon-service gira sempre in background su :11100; ogni
StartDemoClient intercettato viene salvato in uno store persistente
(~/.local/share/veyon-demo-viewer/demos.json) cosi' ogni demo resta
accessibile con il suo token. La finestra mostra stato, demo catturate,
log eventi; l'auto-apertura lancia demo_viewer.py ad ogni nuovo token.

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
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gtk

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

    def deactivate(self, host=None):
        changed = False
        for k, rec in self.demos.items():
            if rec.get("active") and (host is None or k.startswith(host)):
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
            "%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.bridge.put(("log", {"line": self.format(record)}))
        except Exception:
            pass


# ------------------------------------------------------------------ GUI

class DemoRow(Gtk.ListBoxRow):
    def __init__(self, rec, on_open):
        super().__init__()
        self.rec = rec
        box = Gtk.Box(spacing=8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        title = f"{rec['host']}:{rec['port']}"
        mode = "schermo intero" if rec["mode"] == "fullscreen" else "finestra"
        ts = time.strftime("%H:%M:%S", time.localtime(rec["last"]))
        state = "ATTIVA" if rec.get("active") else "terminata"
        box.append(Gtk.Label(label=title, xalign=0))
        box.append(Gtk.Label(
            label=f"{mode} — token delle {ts} — {state}",
            xalign=0, hexpand=True))
        btn = Gtk.Button(label="Apri viewer")
        btn.connect("clicked", lambda *_: on_open(rec))
        box.append(btn)
        self.set_child(box)


class DemoApp(Gtk.Application):
    def __init__(self, args):
        super().__init__(application_id="dev.local.veyon-demo-gui")
        self.args = args
        self.bridge = queue.Queue()
        self.store = DemoStore(STORE_PATH)
        self.viewer_proc = None
        self.rows = {}

    def do_startup(self):
        Gtk.Application.do_startup(self)

    def do_activate(self):
        self.win = Gtk.ApplicationWindow(
            application=self, title="Veyon Demo — servizio")
        self.win.set_default_size(720, 520)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(10)
        root.set_margin_end(10)

        self.status_lbl = Gtk.Label(
            label=f"avvio servizio su :{self.args.port} …", xalign=0)
        self.masters_lbl = Gtk.Label(label="master connessi: 0", xalign=0)
        root.append(self.status_lbl)
        root.append(self.masters_lbl)

        self.auto_sw = Gtk.CheckButton(
            label="Apri automaticamente il viewer ad ogni nuova demo")
        self.auto_sw.set_active(True)
        root.append(self.auto_sw)

        root.append(Gtk.Label(label="Presentazioni catturate:", xalign=0))
        sw = Gtk.ScrolledWindow(vexpand=False, min_content_height=110)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        sw.set_child(self.listbox)
        root.append(sw)

        root.append(Gtk.Label(label="Eventi:", xalign=0))
        logsw = Gtk.ScrolledWindow(vexpand=True)
        self.logview = Gtk.TextView(editable=False, monospace=True)
        self.logview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        logsw.set_child(self.logview)
        root.append(logsw)

        self.win.set_child(root)
        self.win.connect("close-request", self._on_close)
        self.win.present()

        # backend asyncio in thread
        self.backend = Backend(self.args.port, self.bridge, self.store,
                               self.args.verbose)
        self.backend.start()

        # log -> textview
        handler = LogBridge(self.bridge)
        handler.setLevel(logging.INFO if not self.args.verbose
                         else logging.DEBUG)
        logging.getLogger().addHandler(handler)

        GLib.timeout_add(150, self._poll_bridge)
        for rec in self.store.sorted():
            self.add_row(rec)
        self.log_line("servizio avviato — in attesa del veyon-master "
                      f"sulla porta {self.args.port}")

    # ---- helpers UI ---------------------------------------------------------

    def log_line(self, text):
        buf = self.logview.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.logview.scroll_to_mark(mark, 0.0, False, 0.0, 1.0)

    def add_row(self, rec):
        key = DemoStore.key(rec["host"], rec["port"])
        if key in self.rows:
            self.listbox.remove(self.rows[key])
        row = DemoRow(rec, self.open_viewer)
        self.rows[key] = row
        self.listbox.prepend(row)

    def refresh_row(self, rec):
        self.add_row(rec)

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

    def update_status(self):
        self._refresh_masters()

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
            self.log_line(payload["line"])
        elif kind == "conn":
            self.log_line(
                f"master online: {payload['user'] or '?'} ({payload['ip']})")
            self._refresh_masters()
        elif kind == "disc":
            self.log_line(f"master disconnesso: {payload['ip']}")
            self._refresh_masters()
        elif kind == "demo":
            rec = payload
            self.refresh_row(rec)
            self.log_line(
                f"nuova demo su {rec['host']}:{rec['port']} "
                f"({rec['mode']}) — token catturato")
            if self.auto_sw.get_active():
                self.open_viewer(rec)
        elif kind == "demo_stop":
            self.log_line("presentazione terminata — chiudo il viewer")
            self.kill_viewer()
            for rec in self.store.demos.values():
                if not rec.get("active"):
                    self.refresh_row(rec)
        elif kind == "fatal":
            self.status_lbl.set_text(f"ERRORE servizio: {payload['error']}")
            self.log_line(f"FATALE: {payload['error']}")

    def _refresh_masters(self):
        # masters vive nel thread backend: accesso read-only tollerato
        masters = getattr(self.backend, "ctx", None)
        masters = masters.masters if masters else {}
        txt = ("; ".join(f"{u or '?'} ({ip})" for ip, u in masters.items())
               if masters else "nessuno")
        self.masters_lbl.set_text(f"master connessi: {txt}")

    def _on_close(self, *_):
        self.kill_viewer()
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        level=logging.DEBUG if args.verbose else logging.INFO)

    app = DemoApp(args)
    app.run(sys.argv[:1])


if __name__ == "__main__":
    main()
