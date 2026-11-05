"""meet_view.py - tab Meet: login Google + Classroom + viewer Meet.

Meet e' WebRTC proprietario: non si puo' aprire come stream Veyon, si
usa un browser WebKitGTK embedded in una finestra dedicata ("Meet").

Flusso:
  Apri Classroom -> login Google nel browser (sessione persistente in
  ~/.local/share/veyon-demo-viewer/webkit) -> "Aggiorna corsi" legge la
  lista da classroom.google.com -> doppio click su un corso: apre la
  pagina, trova il link Meet -> entra nella riunione e resta collegato
  finche' non si preme Esci (o si chiude la finestra).

Il pannello partecipanti elenca i nomi letti dal DOM di Meet
(best-effort): non e' possibile selezionare/pinnare uno stream via
codice — la selezione di chi guardare si fa nella UI di Meet.

Registrazione: su X11 ffmpeg -f x11grab -window_id sulla finestra Meet;
su Wayland ScreenCast portal con scelta finestra -> frame -> ffmpeg.
"""

import json
import logging
import os
import re
import urllib.parse

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
    HAVE_WK = True
except (ValueError, ImportError):
    WebKit = None
    HAVE_WK = False

from recorder import (PipeRecorder, X11GrabRecorder, is_x11, new_path,
                      window_xid, ffmpeg_ok)
from screen_capture import ScreenCapture

log = logging.getLogger("meet_view")

WEBKIT_DIR = os.path.expanduser("~/.local/share/veyon-demo-viewer/webkit")
CLASSROOM_HOME = "https://classroom.google.com/"

# UA da Chrome desktop: Google rifiuta UA "sconosciuti" su login e Meet
CHROME_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ------------------------------------------------------------- JS ----

JS_COURSES = r"""
(function(){
  var out = {};
  document.querySelectorAll('a').forEach(function(a){
    var h = a.getAttribute('href') || '';
    if (h.indexOf('/c/') === 0 ||
        h.indexOf('https://classroom.google.com/c/') === 0) {
      var t = (a.innerText || '').trim();
      if (!t) return;
      var lines = t.split('\n').filter(function(s){return s.trim();});
      out[h] = {name: lines[0] || h, detail: lines.slice(1).join(' — ')};
    }
  });
  return JSON.stringify(Object.keys(out).map(function(k){
    var c = out[k]; c.href = k; return c;}));
})()
"""

JS_MEET_LINKS = r"""
(function(){
  var s = {};
  document.querySelectorAll('a[href*="meet.google.com"]').forEach(
    function(a){ s[a.href] = 1; });
  return JSON.stringify(Object.keys(s));
})()
"""

JS_JOIN = r"""
(function(){
  var labels = ['partecipa ora','chiedi di partecipare','unisciti ora',
                'join now','ask to join','partecipa','join'];
  var els = document.querySelectorAll(
    'button,[role="button"],a[role="button"],div[role="button"],span[role="button"]');
  for (var i = 0; i < els.length; i++) {
    var b = els[i];
    var t = ((b.getAttribute('aria-label')||'') + ' ' +
             (b.innerText||'')).toLowerCase();
    for (var j = 0; j < labels.length; j++) {
      if (t.indexOf(labels[j]) >= 0) { b.click(); return labels[j]; }
    }
  }
  return '';
})()
"""

JS_INCALL = r"""
(function(){
  var els = document.querySelectorAll('button,[role="button"],[aria-label]');
  for (var i = 0; i < els.length; i++) {
    var a = (els[i].getAttribute('aria-label') || '').toLowerCase();
    if (/abbandona|leave|end call|termina la chiamata/.test(a))
      return 'in-call';
  }
  return '';
})()
"""

JS_PARTICIPANTS = r"""
(function(){
  var out = []; var seen = {};
  document.querySelectorAll('[data-participant-id]').forEach(function(el){
    var n = (el.getAttribute('aria-label') || el.innerText || '')
            .trim().split('\n')[0].trim();
    if (n && !seen[n]) { seen[n] = 1; out.push(n); }
  });
  return JSON.stringify(out);
})()
"""

JS_HANGUP = r"""
(function(){
  var els = document.querySelectorAll('button,[role="button"],[aria-label]');
  for (var i = 0; i < els.length; i++) {
    var a = (els[i].getAttribute('aria-label') || '').toLowerCase();
    if (/abbandona|leave|end call|termina la chiamata/.test(a)) {
      els[i].click(); return 'bye';
    }
  }
  return '';
})()
"""

# ---------------------------------------------------------------------


class MeetWindow(Gtk.Window):
    """Finestra dedicata che ospita la WebView (Meet/Classroom).

    Separata dalla finestra principale: su X11 la registrazione
    -window_id cattura esattamente questa finestra, e resta collegata
    finche' non viene chiusa."""

    def __init__(self, app, on_close):
        super().__init__(application=app, title="Meet")
        self.set_default_size(1100, 720)
        self._on_close_cb = on_close

        os.makedirs(WEBKIT_DIR, exist_ok=True)
        try:
            session = WebKit.NetworkSession.new(
                WEBKIT_DIR, os.path.join(WEBKIT_DIR, "cache"))
        except TypeError:
            session = WebKit.NetworkSession(
                data_directory=WEBKIT_DIR,
                cache_directory=os.path.join(WEBKIT_DIR, "cache"))
        try:
            self.web = WebKit.WebView(network_session=session)
        except TypeError:
            self.web = WebKit.WebView.new_with_context_and_network_session(
                WebKit.WebContext.get_default(), session)
        s = self.web.get_settings()
        s.set_user_agent(CHROME_UA)
        s.set_enable_media_stream(True)
        s.set_enable_webrtc(True)
        s.set_enable_developer_extras(False)
        # richieste permessi (mic/cam/notifiche) auto-concesse:
        # la finestra e' dedicata a Meet e il profilo e' isolato
        self.web.connect("permission-request",
                         lambda _w, req: (req.allow(), True)[1])
        # i target=_blank (es. link meet da classroom) restano in window
        self.web.connect("create", lambda _w, _a: self.web)
        self.set_child(self.web)
        self.connect("close-request", self._closed)

    def _closed(self, *_):
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            cb()
        return False

    def eval_js(self, script, cb):
        """cb(result_obj|None)."""
        def done(_w, res, _u):
            try:
                v = self.web.evaluate_javascript_finish(res)
                cb(v.to_string() if v is not None else None)
            except Exception:
                cb(None)
        self.web.evaluate_javascript(script, -1, None, None, None,
                                     done, None)


class MeetPanel(Gtk.Box):
    """Pannello di controllo Meet/Classroom per la tab principale."""

    def __init__(self, app, log_cb):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.app = app
        self.log = log_cb
        self.meet_win = None
        self.courses = []               # [{href,name,detail}]
        self.selected = None            # href corso selezionato
        self.in_call = False
        self._pending_meet = False      # attesa scrape meet-link
        self._joined = False            # join click gia' tentato/riuscito
        self.rec = None                 # recorder attivo
        self.rec_cap = None             # ScreenCapture (wayland)
        self._rec_timer = None
        self._tick = None
        self._course_rows = []
        self._part_rows = []

        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(18)

        if not HAVE_WK:
            grp = Adw.PreferencesGroup(title="Meet")
            row = Adw.ActionRow(
                title="WebKitGTK 6.0 mancante",
                subtitle="installa il pacchetto gir1.2-webkit-6.0 "
                         "per usare il browser Meet integrato")
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "dialog-warning-symbolic"))
            grp.add(row)
            self.append(grp)
            return

        # ---- gruppo Classroom ----
        grp = Adw.PreferencesGroup(
            title="Google Classroom",
            description="il login Google si fa nella finestra Meet — "
                        "la sessione resta salvata")
        row = Adw.ActionRow(
            title="Browser Classroom",
            subtitle="apri classroom.google.com e accedi a Google")
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "web-browser-symbolic"))
        btn = Gtk.Button(label="Apri", valign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda *_: self.open_classroom())
        row.add_suffix(btn)
        grp.add(row)

        row = Adw.ActionRow(
            title="Corsi",
            subtitle="elenco letto dalla pagina classroom aperta")
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "format-justify-left-symbolic"))
        ref = Gtk.Button(icon_name="view-refresh-symbolic",
                         valign=Gtk.Align.CENTER)
        ref.add_css_class("flat")
        ref.set_tooltip_text("rilegge l'elenco corsi dalla pagina")
        ref.connect("clicked", lambda *_: self.refresh_courses())
        row.add_suffix(ref)
        grp.add(row)

        self.course_scroll = Gtk.ScrolledWindow(
            min_content_height=60, max_content_height=180,
            propagate_natural_height=True)
        self.course_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.course_list.add_css_class("boxed-list")
        self.course_list.connect("row-activated", self._course_activated)
        self.course_list.connect("row-selected", self._course_selected)
        self.course_scroll.set_child(self.course_list)
        grp.add(self.course_scroll)
        self.append(grp)

        # ---- gruppo Meet ----
        mgrp = Adw.PreferencesGroup(title="Riunione Meet")
        self.meet_status = Adw.ActionRow(
            title="Stato", subtitle="nessuna riunione")
        self.meet_status.add_prefix(Gtk.Image.new_from_icon_name(
            "camera-video-symbolic"))
        self.join_btn = Gtk.Button(label="Apri Meet",
                                   valign=Gtk.Align.CENTER,
                                   sensitive=False)
        self.join_btn.add_css_class("suggested-action")
        self.join_btn.set_tooltip_text(
            "entra nel Meet del corso selezionato")
        self.join_btn.connect("clicked", lambda *_: self.open_selected())
        self.leave_btn = Gtk.Button(label="Esci",
                                    valign=Gtk.Align.CENTER,
                                    sensitive=False)
        self.leave_btn.add_css_class("destructive-action")
        self.leave_btn.set_tooltip_text("abbandona la riunione")
        self.leave_btn.connect("clicked", lambda *_: self.leave())
        bx = Gtk.Box(spacing=6)
        bx.append(self.join_btn)
        bx.append(self.leave_btn)
        bx.set_valign(Gtk.Align.CENTER)
        self.meet_status.add_suffix(bx)
        mgrp.add(self.meet_status)

        lrow = Adw.ActionRow(
            title="Link Meet diretto",
            subtitle="meet.google.com/… — utile anche senza login "
                     "(ingresso come ospite)")
        lrow.add_prefix(Gtk.Image.new_from_icon_name(
            "insert-link-symbolic"))
        self.link_entry = Gtk.Entry(placeholder_text="meet.google.com/xxx-xxxx-xxx",
                                    valign=Gtk.Align.CENTER, hexpand=True)
        lrow.add_suffix(self.link_entry)
        ob = Gtk.Button(label="Apri", valign=Gtk.Align.CENTER)
        ob.connect("clicked", lambda *_: self.open_link())
        lrow.add_suffix(ob)
        mgrp.add(lrow)

        self.rec_row = Adw.ActionRow(
            title="Registra riunione",
            subtitle="salva la finestra Meet in .mkv — OFF")
        self.rec_row.add_prefix(Gtk.Image.new_from_icon_name(
            "media-record-symbolic"))
        self.rec_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.rec_sw.connect("notify::active", self._on_rec_sw)
        self.rec_row.add_suffix(self.rec_sw)
        self.rec_row.set_activatable_widget(self.rec_sw)
        mgrp.add(self.rec_row)
        self.append(mgrp)

        # ---- partecipanti ----
        pgrp = Adw.PreferencesGroup(
            title="Partecipanti",
            description="nomi rilevati dal DOM di Meet — per fissare "
                        "uno stream usa il pin nella finestra Meet")
        self.part_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE)
        self.part_list.add_css_class("boxed-list")
        pscroll = Gtk.ScrolledWindow(
            min_content_height=50, max_content_height=160,
            propagate_natural_height=True)
        pscroll.set_child(self.part_list)
        pgrp.add(pscroll)
        self.append(pgrp)

    # ------------------------------------------------------- browser --

    def _ensure_window(self):
        if self.meet_win is None:
            self.meet_win = MeetWindow(self.app, self._win_closed)
            self.meet_win.web.connect("load-changed", self._load_changed)
            if self._tick is None:
                self._tick = GLib.timeout_add(2000, self._meet_tick)
        if not self.meet_win.get_visible():
            self.meet_win.present()
        return self.meet_win

    def _win_closed(self):
        if self.rec:
            self._stop_meet_rec()
        self.in_call = False
        self._joined = False
        self.meet_win = None
        if self._tick:
            GLib.source_remove(self._tick)
            self._tick = None
        self._set_state("nessuna riunione")
        self.leave_btn.set_sensitive(False)
        self.log("meet: finestra chiusa — riunione terminata")

    def open_classroom(self):
        w = self._ensure_window()
        w.web.load_uri(CLASSROOM_HOME)
        self._set_state("classroom aperta — fai login se serve")
        self.log("meet: aperta classroom.google.com")

    def refresh_courses(self):
        w = self.meet_win
        if w is None:
            self._set_state("apri prima il browser Classroom")
            return
        uri = w.web.get_uri() or ""
        if "classroom.google.com" not in uri:
            self._set_state("vai su classroom.google.com poi riprova")
            w.web.load_uri(CLASSROOM_HOME)
            return
        w.eval_js(JS_COURSES, self._courses_done)

    def _courses_done(self, res):
        if not res:
            self._set_state("nessun corso trovato — sei loggato?")
            return
        try:
            self.courses = json.loads(res)
        except Exception:
            self.courses = []
        self._fill_courses()
        self._set_state(f"{len(self.courses)} corsi trovati")
        self.log(f"meet: {len(self.courses)} corsi da classroom")

    def _fill_courses(self):
        for r in self._course_rows:
            self.course_list.remove(r)
        self._course_rows = []
        for c in self.courses:
            row = Adw.ActionRow(title=c["name"],
                                subtitle=c.get("detail") or c["href"])
            row._meet_href = c["href"]
            self.course_list.append(row)
            self._course_rows.append(row)

    def _course_selected(self, _lb, row):
        self.selected = getattr(row, "_meet_href", None) if row else None
        self.join_btn.set_sensitive(self.selected is not None)

    def _course_activated(self, _lb, row):
        self._course_selected(_lb, row)
        self.open_selected()

    # --------------------------------------------------------- meet --

    def open_selected(self):
        """Apre la pagina del corso, trova il link Meet e ci entra."""
        if not self.selected:
            return
        w = self._ensure_window()
        href = self.selected
        url = (href if href.startswith("http")
               else urllib.parse.urljoin(CLASSROOM_HOME, href))
        self._pending_meet = True
        self._set_state("cerco il link Meet nel corso…")
        w.web.load_uri(url)

    def open_link(self):
        link = self.link_entry.get_text().strip()
        if not link:
            return
        if not link.startswith("http"):
            link = "https://" + link
        w = self._ensure_window()
        w.web.load_uri(link)
        self._set_state("apertura meet…")
        self.log(f"meet: apertura {link}")

    def _load_changed(self, web, event):
        if event != WebKit.LoadEvent.FINISHED:
            return
        uri = web.get_uri() or ""
        if self._pending_meet and "classroom.google.com" in uri:
            self._pending_meet = False
            self.meet_win.eval_js(JS_MEET_LINKS, self._meet_links_done)
        elif "meet.google.com/" in uri:
            self._set_state("riunione Meet — join automatico…")
            self.leave_btn.set_sensitive(True)

    def _meet_links_done(self, res):
        try:
            links = json.loads(res) if res else []
        except Exception:
            links = []
        if not links:
            self._set_state("nessun link Meet nel corso "
                            "(il docente non l'ha abilitato?)")
            self.log("meet: nessun link nella pagina corso")
            return
        self.meet_win.web.load_uri(links[0])
        self._set_state("apertura riunione…")
        self.log(f"meet: link trovato {links[0]}")

    def _meet_tick(self):
        w = self.meet_win
        if w is None:
            return False
        uri = w.web.get_uri() or ""
        if "meet.google.com/" not in uri:
            if self.in_call:
                self.in_call = False
                self._joined = False
                self._set_state("riunione terminata")
                self.leave_btn.set_sensitive(False)
            return True
        w.eval_js(JS_INCALL, lambda r: self._incall_done(bool(r)))
        if not self._joined:
            w.eval_js(JS_JOIN, self._join_done)
        if self.in_call:
            w.eval_js(JS_PARTICIPANTS, self._parts_done)
        return True

    def _join_done(self, res):
        if res:
            self._joined = True
            self.log(f"meet: click su «{res}» — join in corso")
            self._set_state("join in corso…")

    def _incall_done(self, yes):
        if yes and not self.in_call:
            self.in_call = True
            self._set_state("in riunione — collegato")
            self.leave_btn.set_sensitive(True)
            self.log("meet: in riunione")
        elif yes:
            self._joined = True         # non riprovare il join

    def _parts_done(self, res):
        try:
            names = json.loads(res) if res else []
        except Exception:
            names = []
        for r in self._part_rows:
            self.part_list.remove(r)
        self._part_rows = []
        for n in names:
            row = Adw.ActionRow(title=n)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                "avatar-default-symbolic"))
            self.part_list.append(row)
            self._part_rows.append(row)

    def leave(self):
        """Esci dalla riunione: click hang-up + torna su classroom."""
        w = self.meet_win
        if w is None:
            return
        if self.rec:
            self._stop_meet_rec()
            self.rec_sw.set_active(False)
        w.eval_js(JS_HANGUP, lambda _r: None)
        w.web.load_uri(CLASSROOM_HOME)
        self.in_call = False
        self._joined = False
        self.leave_btn.set_sensitive(False)
        self._set_state("riunione terminata")
        self.log("meet: uscito dalla riunione")

    def _set_state(self, txt):
        self.meet_status.set_subtitle(txt)

    # ---------------------------------------------------- registrazione --

    def _on_rec_sw(self, sw, _pspec):
        if sw.get_active():
            if not ffmpeg_ok():
                self._set_state("ffmpeg non trovato")
                sw.set_active(False)
                return
            if self.meet_win is None:
                self._set_state("apri prima la finestra Meet")
                sw.set_active(False)
                return
            self._start_meet_rec()
        else:
            self._stop_meet_rec()

    def _start_meet_rec(self):
        path = new_path("meet-rec")
        if is_x11():
            xid = window_xid(self.meet_win)
            if xid:
                self.rec = X11GrabRecorder(path, window_id=xid)
                self.rec.start()
                self.log(f"meet: registrazione finestra -> {path}")
            else:
                self._set_state("XID finestra non trovato")
                self.rec_sw.set_active(False)
                return
        else:
            # Wayland: portale ScreenCast — il dialogo chiede di
            # scegliere la sorgente: selezionare la finestra Meet
            self.rec_cap = ScreenCapture(
                on_status=lambda t: self.log(f"schermo: {t}"),
                source_types=2)
            self.rec_cap.start()
            self._rec_path = path
            self._rec_timer = GLib.timeout_add(100, self._rec_push)
            self.log("meet: scegli la finestra Meet nel dialogo di "
                     "sistema per avviare la registrazione")
        self.rec_row.set_subtitle(f"→ {os.path.basename(path)} — REC")
        self._set_state("registrazione in corso")

    def _rec_push(self):
        cap = self.rec_cap
        if cap is None:
            return False
        fr = cap.get_frame()
        if fr is None:
            return True                 # in attesa del consenso
        if self.rec is None:
            h, w, _ = fr.shape
            self.rec = PipeRecorder(self._rec_path, w, h, fps=10)
            self.rec.start()
            self.log(f"meet: registrazione -> {self._rec_path}")
        self.rec.push(fr)
        return True

    def _stop_meet_rec(self):
        if self._rec_timer:
            GLib.source_remove(self._rec_timer)
            self._rec_timer = None
        if self.rec_cap:
            self.rec_cap.stop()
            self.rec_cap = None
        if self.rec:
            self.rec.stop()
            self.log(f"meet: registrazione salvata in {self.rec.path}")
            self.rec = None
        self.rec_row.set_subtitle("salva la finestra Meet in .mkv — OFF")

    def cleanup(self):
        if self.rec:
            self._stop_meet_rec()
        if self.meet_win:
            self.meet_win.destroy()
            self.meet_win = None
        if self._tick:
            GLib.source_remove(self._tick)
            self._tick = None
