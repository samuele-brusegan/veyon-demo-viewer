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
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk

import veyon_proto as vp
from fake_service import ServiceCtx, run_service
from demo_store import DemoStore, STORE_PATH, Settings, \
    ChatStore, CHAT_PATH, ip_net24, local_net24, local_ipv4
from master_client import ClientService, Credentials, scan_subnet, wol
from screen_capture import ScreenCapture
from demo_server import DemoServer, new_token
from window_icon import set_x11_window_icon

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
                    f"Exec=\"{sys.executable}\" "
                    f"\"{os.path.join(HERE, 'veyon_daemon.py')}\"\n"
                    "NoDisplay=true\n")
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


def daemon_running():
    """True se un processo veyon_daemon.py e' gia' attivo."""
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if b"veyon_daemon.py" in f.read():
                        return True
            except OSError:
                continue
    except OSError:
        pass
    return False


# ------------------------------------------------------------------ backend

class Backend(threading.Thread):
    """Loop asyncio con fake service + consumatore eventi demo."""

    def __init__(self, port, bridge, store, verbose, frame_getter=None):
        super().__init__(daemon=True, name="veyon-backend")
        self.port = port
        self.bridge = bridge            # queue.Queue -> GUI
        self.store = store
        self.verbose = verbose
        self.frame_getter = frame_getter   # -> np RGB o None (condivisione schermo)
        self.ctx = None

    async def consume(self, events):
        while True:
            fm, peer = await events.get()
            if fm.uid == vp.UID_CHAT:
                if fm.command in (vp.CMD_CHAT_SHARE_ON,
                                  vp.CMD_CHAT_SHARE_OFF):
                    self.bridge.put(("share_req", {
                        "ip": peer,
                        "on": fm.command == vp.CMD_CHAT_SHARE_ON}))
                elif fm.command == vp.CMD_CHAT_MSG:
                    self.bridge.put(("chat", {
                        "ip": peer,
                        "from": str(fm.args.get(vp.ARG_CHAT_FROM) or ""),
                        "text": str(fm.args.get(vp.ARG_CHAT_TEXT) or "")}))
                continue
            if fm.uid not in vp.DEMO_UIDS:
                continue
            if fm.command == vp.CMD_START_DEMO_CLIENT:
                host = fm.args.get(vp.ARG_DEMO_SERVER_HOST) or peer
                port = int(fm.args.get(vp.ARG_DEMO_SERVER_PORT) or 11400)
                token = fm.args.get(vp.ARG_DEMO_ACCESS_TOKEN) or b""
                mode = ("fullscreen" if fm.uid == vp.DEMO_UID_FULLSCREEN
                        else "window")
                rec = self.store.upsert(host, port, token, mode,
                                        net=ip_net24(peer))
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
        self.ctx.frame_provider = self.frame_getter

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
        self._store_mtime = None
        self.demo_targets = set()       # ip a cui abbiamo inviato una demo
        self._demo_uid = None           # uid usato per l'ultimo StartDemoClient
        self.capture = None             # ScreenCapture condiviso
        self.demo_server = None         # DemoServer (produzione)
        self.demo_token = None
        self._sw_guard = False          # sync switch senza ricorsione
        self.chat_store = ChatStore(CHAT_PATH)
        self.chat_wins = {}             # ip -> widget chat
        self.client_kind = {}           # ip -> {"name","ver"} | "veyon"
        self.client_status_txt = {}     # ip -> ultimo stato connessione

    # ---- costruzione UI ------------------------------------------------------

    def do_activate(self):
        self.win = Adw.ApplicationWindow(
            application=self, title="Veyon (admin)")
        self.win.set_icon_name("veyon-demo-admin")
        self.win.set_default_size(640, 560)

        header = Adw.HeaderBar()
        # XFCE disegna già i controlli nella decorazione della finestra;
        # evita di duplicarli nell'header Adwaita mantenendo il layout.
        desktops = " ".join((os.environ.get("XDG_CURRENT_DESKTOP", ""),
                             os.environ.get("DESKTOP_SESSION", ""))).lower()
        if "xfce" in desktops:
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)
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
        self.stack.add_titled_with_icon(self._build_settings_page(),
                                        "settings", "Impostazioni",
                                        "emblem-system-symbolic")

        # -- gruppo stato --
        grp = Adw.PreferencesGroup(title="Servizio")

        self.svc_row = Adw.ActionRow(
            title="Fake service",
            subtitle=f"in ascolto sulla porta {self.args.port}")
        self.svc_row.add_prefix(Gtk.Image.new_from_icon_name(
            "network-server-symbolic"))
        self.svc_lbl = Gtk.Label(label="avvio…")
        self.svc_lbl.add_css_class("dim-label")
        self.svc_lbl.set_valign(Gtk.Align.CENTER)
        self.svc_row.add_suffix(self.svc_lbl)
        # icona demonietto: verde = nostro daemon attivo,
        # rossa = porta occupata da un processo estraneo
        self.demon_img = Gtk.Image.new_from_file(
            os.path.join(HERE, "assets", "demon-green.svg"))
        self.demon_img.set_pixel_size(30)
        self.demon_img.set_valign(Gtk.Align.CENTER)
        self.demon_img.set_visible(False)
        self.svc_row.add_suffix(self.demon_img)
        grp.add(self.svc_row)

        self.masters_row = Adw.ActionRow(
            title="Master connessi", subtitle="nessuno")
        self.masters_row.add_prefix(Gtk.Image.new_from_icon_name(
            "system-users-symbolic"))
        mchat = Gtk.Button(icon_name="user-available-symbolic",
                           valign=Gtk.Align.CENTER)
        mchat.add_css_class("flat")
        mchat.set_tooltip_text("apri chat col master")
        mchat.connect("clicked", lambda *_: self._open_master_chat())
        self.masters_row.add_suffix(mchat)
        grp.add(self.masters_row)

        self.daemon_row = Adw.ActionRow(
            title="Daemon al login",
            subtitle="cattura i token demo anche ad app chiusa")
        self.daemon_row.add_prefix(Gtk.Image.new_from_icon_name(
            "emblem-system-symbolic"))
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
        self.auto_row.add_prefix(Gtk.Image.new_from_icon_name(
            "media-playback-start-symbolic"))
        self.auto_sw = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.auto_row.add_suffix(self.auto_sw)
        self.auto_row.set_activatable_widget(self.auto_sw)
        self.demo_grp.add(self.auto_row)

        self.viewer_row = Adw.ActionRow(
            title="Riproduzione", subtitle="nessuna demo in corso")
        self.viewer_row.add_prefix(Gtk.Image.new_from_icon_name(
            "media-playback-pause-symbolic"))
        self.viewer_stop = Gtk.Button(label="Ferma",
                                      valign=Gtk.Align.CENTER,
                                      sensitive=False)
        self.viewer_stop.add_css_class("destructive-action")
        self.viewer_stop.set_tooltip_text("chiude il viewer demo")
        self.viewer_stop.connect("clicked", self._on_stop_viewer)
        self.viewer_row.add_suffix(self.viewer_stop)
        self.demo_grp.add(self.viewer_row)

        self.empty_row = Adw.ActionRow(
            title="Nessuna presentazione intercettata",
            subtitle="in attesa di StartDemoClient dal master")
        self.empty_row.add_prefix(Gtk.Image.new_from_icon_name(
            "video-display-symbolic"))
        self.empty_row.set_sensitive(False)
        self.demo_grp.add(self.empty_row)

        content.append(self.demo_grp)

        # -- gruppo log --
        self.log_grp = Adw.ExpanderRow(title="Registro eventi")
        self.log_grp.add_prefix(Gtk.Image.new_from_icon_name(
            "text-x-generic-symbolic"))
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
        GLib.idle_add(self._set_x11_window_icon)
        if self.args.minimized:
            self.win.minimize()

        # backend asyncio in thread
        self.backend = Backend(self.args.port, self.bridge, self.store,
                               self.args.verbose,
                               frame_getter=self._service_frame)
        self.backend.start()

        handler = LogBridge(self.bridge)
        handler.setLevel(logging.INFO if not self.args.verbose
                         else logging.DEBUG)
        logging.getLogger().addHandler(handler)

        GLib.timeout_add(150, self._poll_bridge)
        GLib.timeout_add(500, self._check_viewer)
        GLib.timeout_add(2000, self._reload_store)
        for rec in self.store.sorted():
            self.add_row(rec)
        self.log_line(f"in ascolto su :{self.args.port} — "
                      "attendo il veyon-master")

        act = Gio.SimpleAction.new("client", GLib.VariantType.new("s"))
        act.connect("activate",
                    lambda _a, p: self._client_action(p.get_string()))
        self.add_action(act)
        self._apply_master_enabled()
        self._apply_record_demo()
        self._apply_record_master()

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

    def _set_x11_window_icon(self):
        if os.environ.get("VEYON_NO_X11_ICON"):
            return False
        try:
            set_x11_window_icon(
                self.win, os.path.join(HERE, "assets", "icon-admin.svg"))
        except Exception as e:
            log.debug("icona finestra non impostata: %s", e)
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
        self.cred_row.add_prefix(Gtk.Image.new_from_icon_name(
            "dialog-password-symbolic"))
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

        demo_grp = Adw.PreferencesGroup(
            title="Trasmetti demo",
            description="1 · attiva «Il mio schermo» (o inserisci "
                        "un'altra sorgente)   2 · spunta i client   "
                        "3 · premi Invia")
        self.demo_self_row = Adw.ActionRow(
            title="Il mio schermo",
            subtitle="demo server su :11400 — OFF")
        self.demo_self_row.add_prefix(Gtk.Image.new_from_icon_name(
            "media-record-symbolic"))
        self.demo_self_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.demo_self_sw.set_active(
            bool(self.settings.get("record_demo")))
        self.demo_self_sw.connect("notify::active",
                                  self._on_record_demo_sw)
        self.demo_self_row.add_suffix(self.demo_self_sw)
        self.demo_self_row.set_activatable_widget(self.demo_self_sw)
        demo_grp.add(self.demo_self_row)
        self.demo_src = Adw.EntryRow(
            title="Altra sorgente (host:porta [token-hex])")
        demo_grp.add(self.demo_src)
        row = Adw.ActionRow(
            title="Invia demo ai selezionati",
            subtitle="«Il mio schermo», la sorgente indicata o "
                     "l'ultima demo attiva catturata")
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "media-playback-start-symbolic"))
        send = Gtk.Button(label="Invia", valign=Gtk.Align.CENTER)
        send.add_css_class("suggested-action")
        send.connect("clicked", self._on_send_demo)
        row.add_suffix(send)
        demo_grp.add(row)

        self.demo_state_row = Adw.ActionRow(
            title="Demo inviata", subtitle="nessuna")
        self.demo_state_row.add_prefix(Gtk.Image.new_from_icon_name(
            "media-playback-stop-symbolic"))
        self.demo_stop_btn = Gtk.Button(
            label="Stop", valign=Gtk.Align.CENTER, sensitive=False)
        self.demo_stop_btn.add_css_class("destructive-action")
        self.demo_stop_btn.set_tooltip_text(
            "invia StopDemoClient ai client")
        self.demo_stop_btn.connect("clicked", self._on_stop_demo)
        self.demo_state_row.add_suffix(self.demo_stop_btn)
        demo_grp.add(self.demo_state_row)
        content.append(demo_grp)

        for ip in self.settings.clients():
            self._add_client_row(ip)
        return scroll

    # ---- pagina impostazioni -------------------------------------------------

    def _build_settings_page(self):
        scroll = Gtk.ScrolledWindow()
        clamp = Adw.Clamp(maximum_size=560)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{m}")(18)
        clamp.set_child(content)
        scroll.set_child(clamp)

        grp = Adw.PreferencesGroup(title="Funzionalità")
        row = Adw.ActionRow(
            title="Modalità master",
            subtitle="monitoring client, demo multicast e comandi remoti")
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "system-users-symbolic"))
        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        sw.set_active(bool(self.settings.get("master_enabled")))
        sw.connect("notify::active", self._on_master_sw)
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        grp.add(row)
        content.append(grp)

        rec_grp = Adw.PreferencesGroup(title="Registrazione schermo")
        self.rec_demo_row = Adw.ActionRow(
            title="Produzione demo",
            subtitle="demo server su :11400 col tuo schermo — OFF")
        self.rec_demo_row.add_prefix(Gtk.Image.new_from_icon_name(
            "media-record-symbolic"))
        self.rec_demo_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.rec_demo_sw.set_active(bool(self.settings.get("record_demo")))
        self.rec_demo_sw.connect("notify::active", self._on_record_demo_sw)
        self.rec_demo_row.add_suffix(self.rec_demo_sw)
        self.rec_demo_row.set_activatable_widget(self.rec_demo_sw)
        rec_grp.add(self.rec_demo_row)

        self.rec_master_row = Adw.ActionRow(
            title="Inviare al master/docente",
            subtitle="i master vedono il tuo schermo reale, non nero — OFF")
        self.rec_master_row.add_prefix(Gtk.Image.new_from_icon_name(
            "camera-video-symbolic"))
        self.rec_master_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.rec_master_sw.set_active(
            bool(self.settings.get("record_master")))
        self.rec_master_sw.connect("notify::active",
                                   self._on_record_master_sw)
        self.rec_master_row.add_suffix(self.rec_master_sw)
        self.rec_master_row.set_activatable_widget(self.rec_master_sw)
        rec_grp.add(self.rec_master_row)
        content.append(rec_grp)
        return scroll

    def _on_master_sw(self, sw, _pspec):
        self.settings.set("master_enabled", sw.get_active())
        self._apply_master_enabled()
        self.log_line("modalità master " +
                      ("attiva" if sw.get_active() else "disattivata"))

    def _apply_master_enabled(self):
        on = bool(self.settings.get("master_enabled"))
        page = self.stack.get_child_by_name("master")
        if page is not None:
            if not on and self.stack.get_visible_child() is page:
                self.stack.set_visible_child_name("demo")
            self.stack.get_page(page).set_visible(on)
        for ip in self.client_rows:
            if on:
                self._start_client(ip)
            else:
                self.client_rows[ip][0].set_subtitle("master disabilitato")
        if not on:
            for svc in self.client_svcs.values():
                svc.stop_flag = True
            self.client_svcs.clear()
            self.demo_targets.clear()
            self._update_demo_state()

    # ---- registrazione schermo ----------------------------------------------

    def _on_record_demo_sw(self, sw, _pspec):
        if self._sw_guard:
            return
        self.settings.set("record_demo", sw.get_active())
        self._apply_record_demo()

    def _sync_record_demo_switches(self, on):
        self._sw_guard = True
        try:
            for s in (getattr(self, "rec_demo_sw", None),
                      getattr(self, "demo_self_sw", None)):
                if s is not None and s.get_active() != on:
                    s.set_active(on)
        finally:
            self._sw_guard = False

    def _on_record_master_sw(self, sw, _pspec):
        self.settings.set("record_master", sw.get_active())
        self._apply_record_master()
        self.log_line("registrazione schermo per il master " +
                      ("attiva" if sw.get_active() else "disattivata"))

    def _cap_status(self, text):
        GLib.idle_add(self.log_line, f"schermo: {text}")

    def _ensure_capture(self):
        if self.capture is None:
            self.capture = ScreenCapture(on_status=self._cap_status)
        self.capture.start()

    def _maybe_stop_capture(self):
        if (self.capture and
                not self.settings.get("record_demo") and
                not self.settings.get("record_master")):
            self.capture.stop()
            self.capture = None

    def _service_frame(self):
        """frame_provider del fake service: schermo reale solo se lo
        switch 'inviare al master' e' attivo."""
        if self.settings.get("record_master") and self.capture:
            return self.capture.get_frame()
        return None

    def _apply_record_master(self):
        on = bool(self.settings.get("record_master"))
        if self.backend and self.backend.ctx:
            self.backend.ctx.sharing = on        # riportato nel pong ident
        if on:
            self._ensure_capture()
            self.rec_master_row.set_subtitle(
                "schermo visibile ai master connessi — ON")
        else:
            self.rec_master_row.set_subtitle(
                "i master vedono il tuo schermo reale, non nero — OFF")
        self._maybe_stop_capture()

    def _apply_record_demo(self):
        on = bool(self.settings.get("record_demo"))
        self._sync_record_demo_switches(on)
        if on:
            self._ensure_capture()
            if self.demo_server is None:
                self.demo_token = new_token()
                self.demo_server = DemoServer(
                    11400, self.capture.get_frame, self.demo_token,
                    on_status=self._cap_status)
                self.demo_server.start()
            ip = local_ipv4() or "127.0.0.1"
            sub = (f"in onda su {ip}:11400 — "
                   f"token {self.demo_token.hex()[:8]}…")
            self.rec_demo_row.set_subtitle(sub)
            self.demo_self_row.set_subtitle(sub)
            if not self.demo_src.get_text().strip():
                self.demo_src.set_text(
                    f"{ip}:11400 {self.demo_token.hex()}")
            self.log_line(f"demo server attivo su {ip}:11400")
        else:
            if self.demo_server:
                self.demo_server.stop()
                self.demo_server = None
                self.demo_token = None
            self.rec_demo_row.set_subtitle(
                "demo server su :11400 col tuo schermo — OFF")
            self.demo_self_row.set_subtitle(
                "demo server su :11400 — OFF")
            if f"{local_ipv4()}:11400" in self.demo_src.get_text():
                self.demo_src.set_text("")
        self._maybe_stop_capture()

    # ---- azioni sui client --------------------------------------------------

    def _client_action(self, param):
        act, ip = param.split("|", 1)
        svc = self.client_svcs.get(ip)
        if act == "chat":
            self._open_chat(ip)
        elif act == "shot":
            self._save_shot(ip, svc.fb if svc else None)
        elif act == "msg":
            self._ask_text(ip, "Messaggio", "Invia", self._send_msg)
        elif act in ("lock", "unlock", "reboot", "powerdown"):
            fm = {
                "lock": vp.FeatureMessage(vp.UID_SCREEN_LOCK, 0, {}),
                "unlock": vp.FeatureMessage(vp.UID_SCREEN_LOCK,
                                            vp.CMD_LOCK_STOP, {}),
                "reboot": vp.FeatureMessage(vp.UID_POWER_REBOOT, 0, {}),
                "powerdown": vp.FeatureMessage(vp.UID_POWER_DOWN, 0, {}),
            }[act]
            if svc:
                svc.send_feature(fm)
            self.log_line(f"{act} → {ip}")
        elif act == "wol":
            self._ask_text(ip, "MAC address (AA:BB:CC:DD:EE:FF)", "WoL",
                           self._send_wol)
        elif act in ("share_on", "share_off"):
            cmd = (vp.CMD_CHAT_SHARE_ON if act == "share_on"
                   else vp.CMD_CHAT_SHARE_OFF)
            if svc:
                svc.send_feature(vp.FeatureMessage(vp.UID_CHAT, cmd, {}))
            self.log_line(f"richiesta condivisione schermo "
                          f"{'ON' if act == 'share_on' else 'OFF'} → {ip}")
        elif act == "control":
            self._open_control(ip)

    def _save_shot(self, ip, fb):
        if fb is None:
            self.log_line(f"{ip}: nessun frame da salvare")
            return
        h, w, _ = fb.shape
        pb = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(fb.tobytes()), GdkPixbuf.Colorspace.RGB,
            False, 8, w, h, w * 3)
        pics = os.path.join(os.path.expanduser("~"), "Pictures")
        os.makedirs(pics, exist_ok=True)
        path = os.path.join(pics, f"veyon-{ip}-{int(time.time())}.png")
        pb.savev(path, "png", [], [])
        self.log_line(f"screenshot {ip} → {path}")

    def _ask_text(self, ip, title, btn_label, cb):
        dlg = Adw.Dialog(title=title, content_width=380)
        tvw = Adw.ToolbarView()
        tvw.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(18)
        e = Gtk.Entry()
        box.append(e)
        btn = Gtk.Button(label=btn_label, halign=Gtk.Align.END)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda *_: (cb(ip, e.get_text()), dlg.close()))
        box.append(btn)
        tvw.set_content(box)
        dlg.set_child(tvw)
        dlg.present(self.win)

    def _send_msg(self, ip, text):
        if not text:
            return
        svc = self.client_svcs.get(ip)
        if svc:
            svc.send_feature(vp.FeatureMessage(
                vp.UID_TEXT_MESSAGE, 0,
                {vp.ARG_TEXT: text, vp.ARG_ICON: 1}))
        self.log_line(f"messaggio → {ip}")

    def _send_wol(self, ip, mac):
        if not mac:
            return
        try:
            wol(mac)
            self.log_line(f"WoL → {mac}")
        except Exception as e:
            self.log_line(f"WoL fallito: {e}")

    # ---- chat custom ---------------------------------------------------------

    def _chat_name(self):
        import getpass
        import socket as _s
        return f"{self.creds.username or getpass.getuser()}@{_s.gethostname()}"

    def _chat_ui(self, ip):
        """Finestra chat del peer: la crea (senza presentarla) e
        carica lo storico."""
        w = self.chat_wins.get(ip)
        if w is not None:
            return w
        win = Gtk.Window(title=f"Chat — {ip}", transient_for=self.win,
                         default_width=420, default_height=340)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(10)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        msgs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scrolled.set_child(msgs)
        entry = Gtk.Entry(placeholder_text="Scrivi un messaggio…")
        entry.connect("activate", lambda *_: self._chat_send(ip))
        box.append(scrolled)
        box.append(entry)
        win.set_child(box)
        win.connect("close-request", self._chat_closed, ip)
        w = {"win": win, "msgs": msgs, "entry": entry,
             "scrolled": scrolled}
        self.chat_wins[ip] = w
        for m in self.chat_store.history(ip):
            self._chat_add(ip, m["who"], m["text"], m.get("ts"))
        return w

    def _chat_closed(self, win, ip):
        if self.chat_wins.get(ip, {}).get("win") is win:
            del self.chat_wins[ip]
        return False

    def _chat_add(self, ip, who, text, ts=None):
        w = self._chat_ui(ip)
        when = time.strftime("%H:%M", time.localtime(ts or time.time()))
        mine = who == "io"
        lbl = Gtk.Label(label=f"{when}  {who}: {text}", wrap=True,
                        xalign=1.0 if mine else 0.0,
                        halign=Gtk.Align.END if mine else Gtk.Align.START,
                        selectable=True)
        if mine:
            lbl.add_css_class("dim-label")
        w["msgs"].append(lbl)
        GLib.idle_add(self._chat_scroll, w)

    def _chat_scroll(self, w):
        adj = w["scrolled"].get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _open_chat(self, ip):
        self._chat_ui(ip)["win"].present()

    def _open_master_chat(self):
        ctx = self.backend.ctx if self.backend else None
        ips = sorted((ctx.masters if ctx else {}).keys())
        if not ips:
            self.log_line("chat: nessun master connesso")
            return
        if len(ips) == 1:
            self._open_chat(ips[0])
            return
        # piu' master: piccola selezione
        dlg = Adw.Dialog(title="Chat con…", content_width=320)
        tvw = Adw.ToolbarView()
        tvw.add_top_bar(Adw.HeaderBar())
        lb = Gtk.ListBox()
        lb.add_css_class("boxed-list")
        for m in ("top", "bottom", "start", "end"):
            getattr(lb, f"set_margin_{m}")(12)
        for ip in ips:
            lb.append(Adw.ActionRow(
                title=f"{ctx.masters.get(ip) or '?'} ({ip})"))
        lb.connect("row-activated",
                   lambda _l, r: (self._open_chat(ips[r.get_index()]),
                                  dlg.close()))
        tvw.set_content(lb)
        dlg.set_child(tvw)
        dlg.present(self.win)

    def _chat_send(self, ip):
        w = self.chat_wins.get(ip)
        if not w:
            return
        text = w["entry"].get_text().strip()
        if not text:
            return
        fm = vp.FeatureMessage(vp.UID_CHAT, vp.CMD_CHAT_MSG, {
            vp.ARG_CHAT_FROM: self._chat_name(),
            vp.ARG_CHAT_TEXT: text})
        sent = False
        svc = self.client_svcs.get(ip)
        if svc and svc.sock:
            svc.send_feature(fm)                    # connessione in uscita
            sent = True
        else:
            ctx = self.backend.ctx if self.backend else None
            if ctx and ctx.send_chat(ip, fm):       # master connesso a noi
                sent = True
        if sent:
            w["entry"].set_text("")
            self._chat_add(ip, "io", text)
            self.chat_store.append(ip, "io", text)
        else:
            self.log_line(f"chat: {ip} non raggiungibile")

    def _send_share_reply(self, ip, accepted):
        fm = vp.FeatureMessage(vp.UID_CHAT, vp.CMD_CHAT_SHARE_REPLY,
                               {vp.ARG_SHARE_ACCEPTED: 1 if accepted else 0})
        ctx = self.backend.ctx if self.backend else None
        if ctx:
            ctx.send_chat(ip, fm)
        svc = self.client_svcs.get(ip)
        if svc and svc.sock:
            svc.send_feature(fm)

    def _ask_share_consent(self, ip):
        self.log_line(f"{ip} chiede di vedere il tuo schermo")
        dlg = Adw.AlertDialog(
            heading="Condivisione schermo",
            body=f"Un client custom ({ip}) chiede di vedere il tuo "
                 "schermo. Se accetti, i master connessi riceveranno "
                 "i frame reali invece del framebuffer nero.")
        dlg.add_response("no", "Rifiuta")
        dlg.add_response("yes", "Consenti")
        dlg.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_close_response("no")
        state = {"answered": False}

        def on_resp(_d, resp):
            if state["answered"]:
                return
            state["answered"] = True
            ok = resp == "yes"
            if ok:
                self.rec_master_sw.set_active(True)
            self._send_share_reply(ip, ok)
            self.log_line(f"condivisione con {ip} "
                          + ("accettata" if ok else "rifiutata"))
        dlg.connect("response", on_resp)
        # auto-rifiuto dopo 30s senza risposta
        GLib.timeout_add_seconds(
            30, lambda: (not state["answered"]) and dlg.close())
        dlg.present(self.win)

    def _client_share(self, ip, accepted):
        GLib.idle_add(self._on_share_reply, ip, accepted)

    def _on_share_reply(self, ip, accepted):
        self.log_line(f"condivisione {'accettata' if accepted else 'rifiutata'} "
                      f"da {ip}")
        kind = self.client_kind.get(ip)
        if isinstance(kind, dict):
            kind["share"] = accepted
            self._refresh_client_sub(ip)
        return False

    def _client_chat(self, ip, name, text):
        GLib.idle_add(self._on_chat_in, ip, name, text)

    def _on_chat_in(self, ip, name, text):
        if not text:
            return False
        who = name or ip
        self.chat_store.append(ip, who, text)
        had_win = ip in self.chat_wins
        self._chat_add(ip, who, text)
        if not had_win:
            self._open_chat(ip)                     # mostra al primo messaggio
        self.log_line(f"chat da {who} ({ip}): {text}")
        return False

    # ---- demo multicast -----------------------------------------------------

    def _on_send_demo(self, *_):
        targets = [ip for ip, (_r, _p, chk) in self.client_rows.items()
                   if chk.get_active()]
        src = self.demo_src.get_text().strip()
        rec = None
        if src:
            try:
                parts = src.split()
                host, port = parts[0].rsplit(":", 1)
                rec = {"host": host, "port": int(port),
                       "token": (parts[1] if len(parts) > 1 else ""),
                       "mode": "window"}
                bytes.fromhex(rec["token"] or "00")
            except ValueError:
                self.log_line("sorgente demo non valida "
                              "(formato host:porta [token-hex])")
                return
        else:
            if self.demo_server:
                rec = {"host": local_ipv4() or "127.0.0.1", "port": 11400,
                       "token": self.demo_server.token.hex(),
                       "mode": "window"}
            else:
                rec = next((r for r in self.store.sorted()
                            if r.get("active") and self._rec_visible(r)),
                           None)
        if not targets:
            self.log_line("demo: nessun client selezionato")
        elif rec is None:
            self.log_line("demo: nessuna sorgente — attiva «Il mio "
                          "schermo» o inserisci una sorgente")
        else:
            uid = (vp.DEMO_UID_FULLSCREEN if rec["mode"] == "fullscreen"
                   else vp.DEMO_UID_WINDOW)
            fm = vp.FeatureMessage(uid, vp.CMD_START_DEMO_CLIENT, {
                vp.ARG_DEMO_ACCESS_TOKEN: bytes.fromhex(rec["token"] or ""),
                vp.ARG_DEMO_SERVER_HOST: rec["host"],
                vp.ARG_DEMO_SERVER_PORT: int(rec["port"])})
            sent = 0
            for ip in targets:
                svc = self.client_svcs.get(ip)
                if svc and svc.sock:
                    svc.send_feature(fm)
                    self.demo_targets.add(ip)
                    sent += 1
                else:
                    self.log_line(f"demo: {ip} non connesso — saltato")
            if sent:
                self._demo_uid = uid
            self._update_demo_state()
            self.log_line(
                f"demo {rec['host']}:{rec['port']} → {sent} client")

    def _update_demo_state(self):
        if self.demo_targets:
            self.demo_state_row.set_subtitle(
                "in corso su " + ", ".join(sorted(self.demo_targets)))
            self.demo_stop_btn.set_sensitive(True)
        else:
            self.demo_state_row.set_subtitle("nessuna")
            self.demo_stop_btn.set_sensitive(False)

    def _on_stop_demo(self, *_):
        uid = self._demo_uid or vp.DEMO_UID_WINDOW
        fm = vp.FeatureMessage(uid, vp.CMD_STOP_DEMO_CLIENT, {})
        stopped = 0
        for ip in sorted(self.demo_targets):
            svc = self.client_svcs.get(ip)
            if svc and svc.sock:
                svc.send_feature(fm)
                stopped += 1
            else:
                self.log_line(f"stop demo: {ip} non connesso — saltato")
        self.demo_targets.clear()
        self._update_demo_state()
        self.log_line(f"stop demo → {stopped} client")
        # se la sorgente e' il nostro demo server: espelli i client
        # connessi e ruota il token, cosi' non possono ricollegarsi
        if self.demo_server:
            self.demo_server.revoke()
            tok = self.demo_server.token.hex()
            ip = local_ipv4() or "127.0.0.1"
            sub = f"in onda su {ip}:11400 — token {tok[:8]}…"
            self.rec_demo_row.set_subtitle(sub)
            self.demo_self_row.set_subtitle(sub)
            if f"{ip}:11400" in self.demo_src.get_text():
                self.demo_src.set_text(f"{ip}:11400 {tok}")
            self.log_line("demo server: client espulsi, token rinnovato")

    # ---- controllo remoto ----------------------------------------------------

    def _open_control(self, ip):
        svc = self.client_svcs.get(ip)
        if svc is None or svc.fb is None:
            self.log_line(f"{ip}: non connesso, controllo impossibile")
            return
        if getattr(self, "_ctrl", None):
            self._ctrl["win"].close()
        win = Gtk.Window(title=f"Controllo — {ip}", transient_for=self.win)
        win.set_default_size(960, 600)
        pic = Gtk.Picture()
        pic.set_can_shrink(True)
        win.set_child(pic)

        btns = [0]
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y:
                       svc.send_pointer(btns[0], *self._ctrl_xy(pic, svc, x, y)))
        win.add_controller(motion)
        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", lambda _g, _n, x, y, b=None:
                      self._ctrl_btn(svc, btns, x, y, True))
        click.connect("released", lambda _g, _n, x, y, b=None:
                      self._ctrl_btn(svc, btns, x, y, False))
        win.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, _k, _s:
                     (svc.send_key(True, kv), True)[1])
        keys.connect("key-released", lambda _c, kv, _k, _s:
                     svc.send_key(False, kv))
        win.add_controller(keys)
        win.connect("close-request", lambda *_: setattr(
            self, "_ctrl", None) or False)
        self._ctrl = {"ip": ip, "win": win, "pic": pic}
        self._paint_client(ip, svc.fb)
        win.present()

    def _ctrl_xy(self, pic, svc, x, y):
        fh, fw = svc.fb.shape[:2]
        pw, ph = max(1, pic.get_width()), max(1, pic.get_height())
        return (min(fw - 1, int(x / pw * fw)),
                min(fh - 1, int(y / ph * fh)))

    def _ctrl_btn(self, svc, btns, x, y, down):
        b = 1
        if down:
            btns[0] |= b
        else:
            btns[0] &= ~b
        svc.send_pointer(btns[0], *self._ctrl_xy(self._ctrl["pic"], svc, x, y))

    def _cred_desc(self):
        parts = []
        if self.creds.key_file:
            kn = self.creds.key_name or \
                os.path.basename(self.creds.key_file)
            parts.append(f"KeyFile: {kn}")
        if self.creds.username:
            parts.append(f"Logon: {self.creds.username}")
        if not parts:
            return "nessuna — chiave effimera (solo client custom)"
        return " · ".join(parts)

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
            import socket as _s
            my_ip = None
            try:
                with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as _sock:
                    _sock.connect(("192.0.2.1", 80))
                    my_ip = _sock.getsockname()[0]
            except OSError:
                pass
            def hit(ip):
                if ip == my_ip:
                    return              # il nostro daemon: non e' un client
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
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "computer-symbolic"))
        pic = Gtk.Picture()
        pic.set_size_request(96, 54)
        pic.set_can_shrink(True)
        row.add_prefix(pic)
        chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        chk.set_tooltip_text("seleziona per demo multicast")
        row.add_suffix(chk)
        menu = Gio.Menu()
        for label, act in (("Chat", "chat"),
                           ("Screenshot", "shot"),
                           ("Messaggio…", "msg"),
                           ("Blocca schermo", "lock"),
                           ("Sblocca", "unlock"),
                           ("Riavvia", "reboot"),
                           ("Spegni", "powerdown"),
                           ("Wake-on-LAN…", "wol"),
                           ("Richiedi condivisione schermo", "share_on"),
                           ("Interrompi condivisione", "share_off"),
                           ("Controllo remoto", "control")):
            it = Gio.MenuItem.new(label, "app.client")
            it.set_attribute_value(
                "target", GLib.Variant.new_string(f"{act}|{ip}"))
            menu.append_item(it)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic",
                            valign=Gtk.Align.CENTER)
        mb.add_css_class("flat")
        mb.set_menu_model(menu)
        row.add_suffix(mb)
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
        self.client_kind.pop(ip, None)
        self.client_status_txt.pop(ip, None)
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
                            on_frame=self._client_frame,
                            on_chat=self._client_chat,
                            on_ident=self._client_ident)
        svc.share_state = (
            lambda: bool(self.settings.get("record_master")))
        svc.on_share = self._client_share
        self.client_svcs[ip] = svc
        svc.start()

    def _client_status(self, ip, text):
        GLib.idle_add(self._set_client_status, ip, text)

    def _set_client_status(self, ip, text):
        self.client_status_txt[ip] = text
        self._refresh_client_sub(ip)

    def _refresh_client_sub(self, ip):
        if ip not in self.client_rows:
            return
        text = self.client_status_txt.get(ip, "")
        kind = self.client_kind.get(ip)
        if isinstance(kind, dict):
            text += f" — client custom v{kind.get('ver') or '?'}"
            if kind.get("share"):
                text += " ·condivide lo schermo"
        elif kind == "veyon":
            text += " — veyon reale"
        self.client_rows[ip][0].set_subtitle(text)

    def _client_ident(self, ip, info):
        GLib.idle_add(self._on_ident, ip, info)

    def _on_ident(self, ip, info):
        prev = self.client_kind.get(ip)
        self.client_kind[ip] = info if info is not None else "veyon"
        self._refresh_client_sub(ip)
        if prev != self.client_kind[ip]:
            if info is not None:
                if isinstance(prev, dict) and \
                        prev.get("share") != info.get("share"):
                    self.log_line(f"{ip} ha "
                                  f"{'avviato' if info.get('share') else 'fermato'} "
                                  "la condivisione schermo")
                elif not isinstance(prev, dict):
                    self.log_line(f"{ip}: client custom rilevato "
                                  f"(v{info.get('ver') or '?'})")
            else:
                self.log_line(f"{ip}: nessun pong ident — "
                              "veyon reale o versione troppo vecchia")
        return False

    def _client_frame(self, ip, fb):
        now = time.monotonic()
        if now - self._last_frame.get(ip, 0) < 0.5:
            return
        self._last_frame[ip] = now
        GLib.idle_add(self._paint_client, ip, fb)

    def _paint_client(self, ip, fb):
        h, w, _ = fb.shape
        tex = Gdk.MemoryTexture.new(
            w, h, Gdk.MemoryFormat.R8G8B8,
            GLib.Bytes.new(fb.tobytes()), w * 3)
        if ip in self.client_rows:
            self.client_rows[ip][1].set_paintable(tex)
        ctrl = getattr(self, "_ctrl", None)
        if ctrl and ctrl["ip"] == ip:
            ctrl["pic"].set_paintable(tex)

    # ---- helpers UI ---------------------------------------------------------

    def log_line(self, text):
        buf = self.logview.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.logview.scroll_to_mark(mark, 0.0, False, 0.0, 1.0)

    @staticmethod
    def _rec_net(rec):
        """Rete di provenienza del record: quella salvata alla cattura,
        altrimenti dedotta dall'IP del demo server (record vecchi)."""
        return rec.get("net") or ip_net24(rec.get("host"))

    def _rec_visible(self, rec):
        """Mostra solo le demo catturate sulla rete /24 corrente."""
        cur = local_net24()
        if cur is None:
            return True                 # rete ignota: non filtrare
        rn = self._rec_net(rec)
        return rn is None or rn == cur

    def add_row(self, rec):
        key = DemoStore.key(rec["host"], rec["port"])
        old = self.rows.pop(key, None)
        if old is not None:
            self.demo_grp.remove(old)
        if not self._rec_visible(rec):
            if not self.rows:
                self.empty_row.set_title("Nessuna demo su questa rete")
                self.empty_row.set_subtitle(
                    "le demo catturate su altre reti sono nascoste")
                self.empty_row.set_visible(True)
            return
        self.empty_row.set_title("Nessuna presentazione intercettata")
        self.empty_row.set_subtitle(
            "in attesa di StartDemoClient dal master")
        self.empty_row.set_visible(False)

        mode = "schermo intero" if rec["mode"] == "fullscreen" else "finestra"
        dt = time.strftime("%d/%m/%Y %H:%M", time.localtime(rec["last"]))
        active = rec.get("active")

        row = Adw.ActionRow(title=f"{rec['host']}:{rec['port']}")
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "video-display-symbolic"))
        row.set_subtitle(
            f"{mode} · {dt} · "
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

    def _check_viewer(self):
        """Rileva la chiusura manuale del viewer (Esc/q/Alt-F4)."""
        if self.viewer_proc is not None and \
                self.viewer_proc.poll() is not None:
            self.viewer_proc = None
            self.viewer_key = None
            self._update_viewer_row()
            self.log_line("viewer chiuso")
        return True

    def _update_viewer_row(self):
        running = self.viewer_proc is not None and \
            self.viewer_proc.poll() is None
        if running and self.viewer_key:
            host, port, _ = self.viewer_key
            self.viewer_row.set_subtitle(
                f"demo in corso — {host}:{port}")
            self.viewer_stop.set_sensitive(True)
        else:
            self.viewer_row.set_subtitle("nessuna demo in corso")
            self.viewer_stop.set_sensitive(False)

    def _on_stop_viewer(self, *_):
        self.kill_viewer()
        self.log_line("riproduzione fermata manualmente")

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
        self._update_viewer_row()

    def kill_viewer(self):
        self.viewer_key = None
        if self.viewer_proc and self.viewer_proc.poll() is None:
            self.viewer_proc.terminate()
            try:
                self.viewer_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.viewer_proc.kill()
        self.viewer_proc = None
        self._update_viewer_row()

    # ---- bridge poll ---------------------------------------------------------

    def _reload_store(self):
        """Rileva le scritture del daemon (o di un'altra istanza) sullo
        store condiviso e ricostruisce la lista demo."""
        try:
            mt = os.path.getmtime(self.store.path)
        except OSError:
            mt = None
        if mt == self._store_mtime:
            return True
        self._store_mtime = mt
        self.store.load()
        for row in self.rows.values():
            self.demo_grp.remove(row)
        self.rows.clear()
        if not self.store.demos:
            self.empty_row.set_title("Nessuna presentazione intercettata")
            self.empty_row.set_subtitle(
                "in attesa di StartDemoClient dal master")
            self.empty_row.set_visible(True)
            return True
        for rec in self.store.sorted():
            self.add_row(rec)
        return True

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
                self.svc_lbl.set_visible(True)
                self.demon_img.set_visible(False)
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
        elif kind == "chat":
            self._on_chat_in(payload["ip"], payload.get("from", ""),
                             payload.get("text", ""))
        elif kind == "share_req":
            ip, on = payload["ip"], payload["on"]
            if not on:
                # revoca: immediata, nessun consenso necessario
                if self.rec_master_sw.get_active():
                    self.rec_master_sw.set_active(False)
                self.log_line(f"{ip} ha fermato la condivisione schermo")
            elif self.rec_master_sw.get_active():
                self._send_share_reply(ip, True)   # gia' condiviso
            else:
                self._ask_share_consent(ip)
        elif kind == "ident":
            self._refresh_masters()
            self.log_line(f"master {payload['ip']} è un client custom "
                          f"v{payload.get('version') or '?'}")
        elif kind == "fatal":
            err = payload["error"]
            if "98" in err and daemon_running():
                self.svc_lbl.set_visible(False)
                self.demon_img.set_from_file(
                    os.path.join(HERE, "assets", "demon-green.svg"))
                self.demon_img.set_tooltip_text(
                    "Daemon attivo su :" + str(self.args.port) +
                    " — il nostro servizio in background cattura i "
                    "token demo anche quando questa finestra è chiusa")
                self.demon_img.set_visible(True)
                self.svc_lbl.remove_css_class("dim-label")
                self.svc_lbl.add_css_class("success")
                self.status_dot.add_css_class("success")
                self.svc_row.set_title("Cattura demo")
                self.svc_row.set_subtitle(
                    "gestita dal daemon in background su "
                    f":{self.args.port} — elenco in sola lettura")
                self.log_line(f":{self.args.port} occupata dal daemon — "
                              "seguo lo store condiviso")
            else:
                self.svc_lbl.set_visible(False)
                self.demon_img.set_from_file(
                    os.path.join(HERE, "assets", "demon-red.svg"))
                self.demon_img.set_tooltip_text(
                    f"Servizio non attivo: {err}" +
                    (" — la porta è occupata da un altro processo"
                     if "98" in err else ""))
                self.demon_img.set_visible(True)
                self.svc_lbl.add_css_class("error")
                self.log_line(f"FATALE: {err}")
                if "98" in err:
                    self.svc_row.set_subtitle(
                        "porta occupata — un'altra istanza è già attiva")

    def _refresh_masters(self):
        ctx = self.backend.ctx if self.backend else None
        masters = ctx.masters if ctx else {}
        ident = ctx.master_ident if ctx else {}
        txt = ("; ".join(
                f"{u or '?'} ({ip})" +
                (f" ·custom v{ident[ip]}" if ip in ident else "")
                for ip, u in masters.items())
               if masters else "nessuno")
        self.masters_row.set_subtitle(txt)

    def _on_close(self, *_):
        for svc in self.client_svcs.values():
            svc.stop_flag = True
        if self.demo_server:
            self.demo_server.stop()
        if self.capture:
            self.capture.stop()
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
                    f"Icon={os.path.join(HERE, 'assets', 'icon-admin.svg')}\n"
                    f"StartupWMClass={APP_ID}\n"
                    f"Exec=\"{sys.executable}\" "
                    f"\"{os.path.join(HERE, 'veyondemo_gui.py')}\"\n"
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
