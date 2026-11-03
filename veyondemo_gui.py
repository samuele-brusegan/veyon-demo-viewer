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
from gi.repository import Adw, Gdk, GLib, Gtk

import veyon_proto as vp
from fake_service import ServiceCtx, run_service
from demo_store import DemoStore, STORE_PATH, Settings
from master_client import ClientService, Credentials, scan_subnet

log = logging.getLogger("veyondemo_gui")

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ID = "dev.local.veyon-demo-gui"
AUTOSTART_PATH = os.path.expanduser(
    "~/.config/autostart/dev.local.veyon-daemon.desktop")
DAEMON_ASKED = os.path.join(os.path.dirname(STORE_PATH), "daemon-asked")


def install_autostart():
    """~/.config/autostart/: veyon_daemon parte a ogni login di sessione."""
    try:
        os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
        with open(AUTOSTART_PATH, "w") as f:
            f.write("[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=Veyon Daemon\n"
                    f"Exec={sys.executable} "
                    f"{os.path.join(HERE, 'veyon_daemon.py')}\n"
                    "NoDisplay=true\n"
                    "X-GNOME-Autostart-enabled=true\n")
    except OSError:
        pass


def remove_autostart():
    try:
        os.remove(AUTOSTART_PATH)
    except OSError:
        pass


def mark_asked():
    try:
        os.makedirs(os.path.dirname(DAEMON_ASKED), exist_ok=True)
        open(DAEMON_ASKED, "w").close()
    except OSError:
        pass


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
        super().__init__(application_id=APP_ID)
        self.args = args
        self.bridge = queue.Queue()
        self.store = DemoStore(STORE_PATH)
        self.viewer_proc = None
        self.viewer_key = None
        self.rows = {}
        self.backend = None
        self.settings = Settings()
        self.creds = Credentials()
        self.client_svcs = {}
        self.client_rows = {}
        self._last_frame = {}

    # ---- costruzione UI ------------------------------------------------------

    def do_activate(self):
        self.win = Adw.ApplicationWindow(
            application=self, title="Veyon (admin)")
        self.win.set_icon_name("veyon-demo-admin")
        self.win.set_default_size(640, 560)

        header = Adw.HeaderBar()
        self.status_dot = Gtk.Image(icon_name="media-record-symbolic")
        self.status_dot.set_pixel_size(10)
        header.pack_start(self.status_dot)

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.set_content(self.stack)
        self.win.set_content(tv)

        demo_scroll = Gtk.ScrolledWindow()
        clamp = Adw.Clamp(maximum_size=560)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        clamp.set_child(content)
        demo_scroll.set_child(clamp)
        self.stack.add_titled_with_icon(demo_scroll, "demo", "Demo",
                                        "video-display-symbolic")
        self.stack.add_titled_with_icon(self._build_master_page(),
                                        "master", "Master",
                                        "system-users-symbolic")

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

        self.daemon_row = Adw.ActionRow(
            title="Daemon al login",
            subtitle="cattura i token demo anche ad app chiusa")
        self.daemon_row.set_icon_name("emblem-system-symbolic")
        self.daemon_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.daemon_sw.set_active(os.path.exists(AUTOSTART_PATH))
        self.daemon_sw.connect("notify::active", self._on_daemon_sw)
        self.daemon_row.add_suffix(self.daemon_sw)
        self.daemon_row.set_activatable_widget(self.daemon_sw)
        grp.add(self.daemon_row)
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
        if self.args.minimized:
            self.win.minimize()

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

        if not os.path.exists(AUTOSTART_PATH) and \
                not os.path.exists(DAEMON_ASKED):
            GLib.idle_add(self._ask_daemon)

    # ---- daemon autostart ---------------------------------------------------

    def _ask_daemon(self):
        dlg = Adw.AlertDialog(
            heading="Cattura in background?",
            body="Vuoi che i token demo vengano catturati anche quando "
                 "l'app è chiusa? Il daemon minimale (veyon_daemon.py) "
                 "verrà avviato automaticamente a ogni login.")
        dlg.add_response("no", "Non ora")
        dlg.add_response("yes", "Attiva al login")
        dlg.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("yes")
        dlg.set_close_response("no")
        dlg.connect("response", self._on_daemon_response)
        dlg.present(self.win)
        return False

    def _on_daemon_response(self, dlg, response):
        mark_asked()
        if response == "yes":
            self.daemon_sw.set_active(True)     # fa install_autostart + log

    def _on_daemon_sw(self, sw, _pspec):
        mark_asked()
        if sw.get_active():
            install_autostart()
            self.log_line("daemon in autostart — attivo dal prossimo login")
        else:
            remove_autostart()
            self.log_line("daemon rimosso dall'autostart")

    # ---- pagina master ------------------------------------------------------

    def _build_master_page(self):
        scroll = Gtk.ScrolledWindow()
        clamp = Adw.Clamp(maximum_size=560)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        clamp.set_child(content)
        scroll.set_child(clamp)

        grp = Adw.PreferencesGroup(title="Credenziali")
        self.cred_row = Adw.ActionRow(title="Autenticazione master",
                                    subtitle=self._cred_desc())
        self.cred_row.set_icon_name("dialog-password-symbolic")
        btn = Gtk.Button(label="Configura…", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._on_cred_edit)
        self.cred_row.add_suffix(btn)
        grp.add(self.cred_row)
        content.append(grp)

        self.clients_grp = Adw.PreferencesGroup(title="Client monitorati")
        add_row = Adw.ActionRow(title="Aggiungi o cerca nella rete")
        self.ip_entry = Gtk.Entry(placeholder_text="192.168.x.x",
                                  hexpand=True, valign=Gtk.Align.CENTER)
        self.ip_entry.connect("activate", self._on_add_client)
        add_row.add_suffix(self.ip_entry)
        add_btn = Gtk.Button(icon_name="list-add-symbolic",
                             valign=Gtk.Align.CENTER)
        add_btn.set_tooltip_text("Aggiungi client")
        add_btn.connect("clicked", self._on_add_client)
        add_row.add_suffix(add_btn)
        scan_btn = Gtk.Button(icon_name="find-location-symbolic",
                              valign=Gtk.Align.CENTER)
        scan_btn.set_tooltip_text("Scansione subnet su :11100")
        scan_btn.connect("clicked", self._on_scan)
        add_row.add_suffix(scan_btn)
        self.clients_grp.add(add_row)
        self.empty_clients_row = Adw.ActionRow(
            title="Nessun client",
            subtitle="aggiungi un IP o lancia la scansione")
        self.empty_clients_row.set_sensitive(False)
        self.clients_grp.add(self.empty_clients_row)
        content.append(self.clients_grp)

        demo_grp = Adw.PreferencesGroup(title="Demo multicast")
        row = Adw.ActionRow(
            title="Invia demo ai selezionati",
            subtitle="disabilitato — nessun StartDemoClient verrà inviato")
        row.set_icon_name("media-playback-start-symbolic")
        send = Gtk.Button(label="Invia", valign=Gtk.Align.CENTER)
        send.set_sensitive(False)
        row.add_suffix(send)
        demo_grp.add(row)
        content.append(demo_grp)

        for ip in self.settings.clients():
            self._add_client_row(ip)
        return scroll

    def _cred_desc(self):
        if self.settings.get("key_file"):
            return f"KeyFile: {os.path.basename(self.settings.get('key_file'))}"
        if self.settings.get("password"):
            return f"Logon: {self.settings.get('username') or '?'}"
        return "nessuna — solo client custom / tentativo anonimo"

    def _on_cred_edit(self, *_):
        dlg = Adw.Dialog(title="Credenziali master", content_width=420)
        tvw = Adw.ToolbarView()
        tvw.add_top_bar(Adw.HeaderBar())
        grp = Adw.PreferencesGroup()
        e_key = Adw.EntryRow(title="File chiave privata (PEM)")
        e_kname = Adw.EntryRow(title="Nome chiave")
        e_user = Adw.EntryRow(title="Utente (Logon)")
        e_pass = Adw.PasswordEntryRow(title="Password (Logon)")
        for row, k in ((e_key, "key_file"), (e_kname, "key_name"),
                       (e_user, "username"), (e_pass, "password")):
            row.set_text(self.settings.get(k))
            grp.add(row)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.append(grp)
        save = Gtk.Button(label="Salva", halign=Gtk.Align.END)
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save_creds(
            dlg, e_key, e_kname, e_user, e_pass))
        box.append(save)
        tvw.set_content(box)
        dlg.set_child(tvw)
        dlg.present(self.win)

    def _save_creds(self, dlg, e_key, e_kname, e_user, e_pass):
        for k, e in (("key_file", e_key), ("key_name", e_kname),
                     ("username", e_user), ("password", e_pass)):
            self.settings.set(k, e.get_text())
        self.creds = Credentials()
        self.cred_row.set_subtitle(self._cred_desc())
        for ip in list(self.client_svcs):
            self._start_client(ip)
        dlg.close()
        self.log_line("credenziali master aggiornate — riconnessione")

    def _on_add_client(self, *_):
        ip = self.ip_entry.get_text().strip()
        if ip:
            self._add_client_row(ip)
            self.ip_entry.set_text("")

    def _on_scan(self, btn):
        btn.set_sensitive(False)
        self.log_line("scansione subnet su :11100 in corso…")

        def work():
            n = [0]
            def hit(ip):
                n[0] += 1
                GLib.idle_add(self._add_client_row, ip)
            try:
                scan_subnet(11100, hit)
            except Exception as e:
                GLib.idle_add(self.log_line, f"scansione fallita: {e}")
            GLib.idle_add(self._scan_done, btn, n[0])

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, btn, found):
        btn.set_sensitive(True)
        self.log_line(f"scansione completata: {found} host su :11100")

    def _add_client_row(self, ip):
        if ip in self.client_rows:
            return
        self.empty_clients_row.set_visible(False)
        row = Adw.ActionRow(title=ip, subtitle="connessione…")
        row.set_icon_name("computer-symbolic")
        pic = Gtk.Picture()
        pic.set_size_request(96, 54)
        pic.set_can_shrink(True)
        row.add_prefix(pic)
        chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        chk.set_tooltip_text("seleziona per demo multicast")
        row.add_suffix(chk)
        rm = Gtk.Button(icon_name="user-trash-symbolic",
                        valign=Gtk.Align.CENTER)
        rm.add_css_class("flat")
        rm.connect("clicked", lambda *_: self._remove_client(ip))
        row.add_suffix(rm)
        self.clients_grp.add(row)
        self.client_rows[ip] = (row, pic, chk)
        self._save_clients()
        self._start_client(ip)

    def _remove_client(self, ip):
        svc = self.client_svcs.pop(ip, None)
        if svc:
            svc.stop_flag = True
        row = self.client_rows.pop(ip, None)
        if row:
            self.clients_grp.remove(row[0])
        if not self.client_rows:
            self.empty_clients_row.set_visible(True)
        self._save_clients()

    def _save_clients(self):
        self.settings.set_clients(sorted(self.client_rows))

    def _start_client(self, ip):
        old = self.client_svcs.get(ip)
        if old:
            old.stop_flag = True
        svc = ClientService(ip, self.creds,
                            on_status=self._client_status,
                            on_frame=self._client_frame)
        self.client_svcs[ip] = svc
        svc.start()

    def _client_status(self, ip, text):
        GLib.idle_add(self._set_client_status, ip, text)

    def _set_client_status(self, ip, text):
        if ip in self.client_rows:
            self.client_rows[ip][0].set_subtitle(text)

    def _client_frame(self, ip, fb):
        now = time.monotonic()
        if now - self._last_frame.get(ip, 0) < 0.5:
            return
        self._last_frame[ip] = now
        GLib.idle_add(self._paint_client, ip, fb)

    def _paint_client(self, ip, fb):
        if ip not in self.client_rows:
            return
        h, w, _ = fb.shape
        tex = Gdk.MemoryTexture.new(
            w, h, Gdk.MemoryFormat.R8G8B8,
            GLib.Bytes.new(fb.tobytes()), w * 3)
        self.client_rows[ip][1].set_paintable(tex)

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
        key = (rec["host"], int(rec["port"]), rec["token"])
        if self.viewer_key == key and self.viewer_proc is not None \
                and self.viewer_proc.poll() is None:
            self.log_line(f"viewer già aperto su {rec['host']}:{rec['port']} — "
                          "ignoro il riavvio")
            return
        self.kill_viewer()
        self.log_line(f"apertura viewer su {rec['host']}:{rec['port']}…")
        self.viewer_proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "demo_viewer.py"),
             rec["host"], str(rec["port"]), rec["token"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.viewer_key = key

    def kill_viewer(self):
        self.viewer_key = None
        if self.viewer_proc and self.viewer_proc.poll() is None:
            self.viewer_proc.terminate()
            try:
                self.viewer_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.viewer_proc.kill()
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
            if "98" in payload["error"]:
                self.svc_row.set_subtitle(
                    "porta occupata — il daemon veyon_daemon.py "
                    "o un'altra istanza è già attivo")

    def _refresh_masters(self):
        ctx = self.backend.ctx if self.backend else None
        masters = ctx.masters if ctx else {}
        txt = ("; ".join(f"{u or '?'} ({ip})" for ip, u in masters.items())
               if masters else "nessuno")
        self.masters_row.set_subtitle(txt)

    def _on_close(self, *_):
        for svc in self.client_svcs.values():
            svc.stop_flag = True
        self.kill_viewer()
        return False


def _install_icon():
    theme = os.path.expanduser("~/.local/share/icons/hicolor")
    dst = os.path.join(theme, "scalable/apps")
    try:
        os.makedirs(dst, exist_ok=True)
        import shutil
        shutil.copyfile(os.path.join(HERE, "assets", "icon.svg"),
                        os.path.join(dst, "veyon-demo-viewer.svg"))
        shutil.copyfile(os.path.join(HERE, "assets", "icon-admin.svg"),
                        os.path.join(dst, "veyon-demo-admin.svg"))
    except OSError:
        pass
    cache = os.path.join(theme, "icon-theme.cache")
    if os.path.exists(cache) and not os.access(cache, os.W_OK):
        try:
            os.remove(cache)
        except OSError:
            pass
    import shutil
    if shutil.which("gtk-update-icon-cache"):
        subprocess.run(["gtk-update-icon-cache", "-f", "-t", "-q", theme],
                       check=False)


def _install_desktop_entry():
    apps = os.path.expanduser("~/.local/share/applications")
    try:
        os.makedirs(apps, exist_ok=True)
        with open(os.path.join(apps, APP_ID + ".desktop"), "w") as f:
            f.write("[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=Veyon (admin)\n"
                    "Comment=Cattura e mostra le presentazioni Veyon\n"
                    "Icon=veyon-demo-admin\n"
                    f"Exec={sys.executable} "
                    f"{os.path.join(HERE, 'veyondemo_gui.py')}\n"
                    "Terminal=false\n"
                    "Categories=Network;RemoteAccess;\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-m", "--minimized", action="store_true",
                    help="avvia la finestra minimizzata")
    args = ap.parse_args()

    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        level=logging.DEBUG if args.verbose else logging.INFO)

    Adw.init()
    _install_icon()
    _install_desktop_entry()
    GLib.set_application_name("Veyon (admin)")
    app = DemoApp(args)
    app.run(sys.argv[:1])


if __name__ == "__main__":
    main()
