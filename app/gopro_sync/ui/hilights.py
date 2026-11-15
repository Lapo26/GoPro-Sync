"""Momentos HiLight en el reproductor.

La GoPro guarda un "HiLight" cada vez que se aprieta el boton (o se dice
"GoPro HiLight") mientras graba. Aca se muestran como triangulos amarillos en
la barra de avance, se puede saltar de uno a otro, marcar momentos propios
(se guardan en la biblioteca, el video no se toca) y exportarlos como clips
o como un video resumen.

Atajos: H = siguiente momento, Mayus+H = anterior, M = marcar el momento actual."""
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import QMenu

from ..i18n import tr
from ..library import LIBRARY
from ..telemetry import read_hilights
from ..workers import ScanThread

MENU_STYLE = ("QMenu { background: #23232b; border: 1px solid rgba(255,255,255,0.12); padding: 6px; }"
              " QMenu::item { color: #fff; padding: 7px 16px; border-radius: 6px; }"
              " QMenu::item:selected { background: rgba(255,255,255,0.16); }"
              " QMenu::item:disabled { color: rgba(255,255,255,0.45); }"
              " QMenu::separator { height: 1px; background: rgba(255,255,255,0.1); margin: 4px 8px; }")
LEAD_MS = 2000          # al saltar a un momento se arranca 2 s antes, para verlo venir


def fmt_ms(ms):
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class HiLightExtras(QObject):
    """Se engancha al VideoPlayerDialog: boton de momentos, marcas en la barra
    de avance y atajos de teclado."""

    def __init__(self, player, controls_layout, round_btn):
        super().__init__(player)
        self.pl = player
        self.lang = player.lang
        self.path = None
        self.gen = 0
        self.hilights, self.marks = [], []
        self.btn = round_btn("🔖", tr(self.lang, "moments_btn"))
        self.btn.clicked.connect(self.show_menu)
        idx = controls_layout.indexOf(player.btn_full)
        controls_layout.insertWidget(idx if idx >= 0 else controls_layout.count(), self.btn)
        player.slider_seek.setMinimumHeight(24)      # que entren las marcas de los momentos
        player.player.sourceChanged.connect(self._on_source)
        player.installEventFilter(self)

    # ---- carga (en segundo plano: el indice del video esta al final del archivo)
    def _on_source(self, url):
        path = self.pl._path if url is not None and not url.isEmpty() else ""
        if not path or path == self.path:
            return
        self.path = path
        self.gen += 1
        self.hilights = []
        self.marks = LIBRARY.marks_of(path)
        self._refresh()
        th = ScanThread(self.gen, read_hilights, path)
        th.done.connect(self._on_hilights)
        th.start()
        self._thread = th            # que no lo junte el recolector antes de terminar

    def _on_hilights(self, gen, marks):
        if gen == self.gen:
            self.hilights = list(marks or [])
            self._refresh()

    def _refresh(self):
        s = self.pl.slider_seek
        s.hilights = list(self.hilights)
        s.user_marks = list(self.marks)
        s.update()
        tele = getattr(self.pl, "telemetry_extras", None)
        if tele is not None:
            tele.sync_moments()          # el grafico muestra los mismos momentos
        n = len(self.hilights) + len(self.marks)
        self.btn.setToolTip(tr(self.lang, "moments_btn") + (f"  ({n})" if n else "") + "\n" + tr(self.lang, "moments_keys"))

    def moments(self):
        return sorted([(m, "hl") for m in self.hilights] + [(m, "mark") for m in self.marks])

    # ---- acciones
    def seek(self, ms):
        ms = max(0, int(ms))
        self.pl.slider_seek.setValue(ms)
        self.pl._do_seek(ms)

    def _toast(self, text):
        from ..ui.widgets import show_toast
        show_toast(self.pl, text)

    def step(self, delta):
        pos = self.pl.player.position()
        ms = [m for m, _k in self.moments()]
        if not ms:
            self._toast(tr(self.lang, "moments_none"))
            return
        if delta > 0:
            # el punto de partida es LEAD_MS antes del momento: el siguiente es el que esta despues de eso
            nxt = [m for m in ms if m - LEAD_MS > pos + 300]
            target = nxt[0] if nxt else None
        else:
            prv = [m for m in ms if m - LEAD_MS < pos - 1500]
            target = prv[-1] if prv else None
        if target is None:
            self._toast(tr(self.lang, "moments_last" if delta > 0 else "moments_first"))
            return
        self.seek(target - LEAD_MS)

    def mark_now(self):
        if not self.path:
            return
        pos = self.pl.player.position()
        LIBRARY.add_mark(self.path, pos)
        self.marks = LIBRARY.marks_of(self.path)
        self._refresh()
        self._toast(tr(self.lang, "mark_added", t=fmt_ms(pos)))
        self._notify_gallery()

    def unmark_near(self):
        pos = self.pl.player.position()
        near = [m for m in self.marks if abs(m - pos) <= 5000]
        if not near:
            return
        LIBRARY.remove_mark(self.path, min(near, key=lambda x: abs(x - pos)))
        self.marks = LIBRARY.marks_of(self.path)
        self._refresh()
        self._notify_gallery()

    def _notify_gallery(self):
        gal = self.pl.parent()
        if hasattr(gal, "refresh_hilight_marks"):
            gal.refresh_hilight_marks()

    def export(self):
        par = self.pl.parent()
        main = par.window() if par is not None else None
        if main is None or not hasattr(main, "export_hilights"):
            return
        self.pl.player.pause()
        main.export_hilights(self.path, [m for m, _k in self.moments()], parent=self.pl)

    def show_menu(self):
        menu = QMenu(self.pl)
        menu.setStyleSheet(MENU_STYLE)
        moments = self.moments()
        head = menu.addAction(tr(self.lang, "moments_title", n=len(moments)) if moments
                              else tr(self.lang, "moments_none"))
        head.setEnabled(False)
        for ms, kind in moments:
            menu.addAction(tr(self.lang, "moment_hl" if kind == "hl" else "moment_mark", t=fmt_ms(ms)),
                           lambda ms=ms: self.seek(ms - LEAD_MS))
        menu.addSeparator()
        menu.addAction(tr(self.lang, "mark_now"), self.mark_now)
        pos = self.pl.player.position()
        if any(abs(m - pos) <= 5000 for m in self.marks):
            menu.addAction(tr(self.lang, "mark_remove"), self.unmark_near)
        exp = menu.addAction(tr(self.lang, "hl_export_menu"), self.export)
        exp.setEnabled(bool(moments))
        menu.exec(self.btn.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    # ---- teclado
    def eventFilter(self, obj, event):
        if obj is self.pl and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_H:
                self.step(-1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
                return True
            if event.key() == Qt.Key.Key_M:
                self.mark_now()
                return True
        return False


class HiLightActionsMixin:
    """Metodos de la ventana principal para exportar los momentos."""

    def _hilight_dialog(self, parent, n_moments, n_videos=1):
        import os
        from ..env import save_config
        from ..videotools import share_dir
        from ..ui.feature_dialogs import HiLightExportDialog
        start = self.config.get("hl_last_dir") or share_dir()
        if not os.path.isdir(start):
            start = share_dir()
        dlg = HiLightExportDialog(parent or self, self.lang, self.get_current_theme_color(), n_moments, start,
                                  self.config.get("hl_export"), n_videos=n_videos)
        if not dlg.exec():
            return None
        opts = dlg.options()
        self.config["hl_export"] = opts
        self.config["hl_last_dir"] = dlg.out_dir
        save_config(self.config)
        return opts, dlg.out_dir

    def _hilight_done(self, ok, stats):
        import os
        from ..winshell import show_in_explorer
        outs = stats.get("outputs") or []
        if outs:
            show_in_explorer(outs[0])
            target = os.path.normcase(os.path.abspath(self.sync_page.sync_target_dir))
            if os.path.normcase(os.path.abspath(outs[0])).startswith(target):
                self.videos_page.refresh()

    def export_hilights(self, path, marks, parent=None):
        """Momentos de un video (desde el reproductor o el clic derecho)."""
        import os
        from ..videotools import hilight_export_job
        from ..ui.widgets import show_toast
        if not marks:
            show_toast(parent or self, tr(self.lang, "moments_none"))
            return False
        got = self._hilight_dialog(parent, len(marks))
        if not got:
            return False
        opts, out_dir = got
        job = hilight_export_job(path, marks, opts["before"], opts["after"], opts["mode"], opts["single"],
                                 self.lang, out_dir)
        self.sync_page.start_tool(tr(self.lang, "hl_task", name=os.path.basename(path)), [job], self._hilight_done)
        if parent is not None and parent is not self:
            show_toast(parent, tr(self.lang, "clip_started"))
        else:
            self.show_activity()
        return True

    def export_hilights_many(self, paths, parent=None):
        """Momentos de varios videos elegidos en la galeria (un trabajo por video)."""
        from PySide6.QtWidgets import QApplication
        from ..videotools import hilight_export_job
        from ..ui.widgets import show_toast
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = []
            for p in paths:
                ms = sorted(set(read_hilights(p)) | set(LIBRARY.marks_of(p)))
                if ms:
                    found.append((p, ms))
        finally:
            QApplication.restoreOverrideCursor()
        if not found:
            show_toast(parent or self, tr(self.lang, "hl_none_selected"))
            return False
        got = self._hilight_dialog(parent, sum(len(ms) for _p, ms in found), n_videos=len(found))
        if not got:
            return False
        opts, out_dir = got
        jobs = [hilight_export_job(p, ms, opts["before"], opts["after"], opts["mode"], opts["single"], self.lang, out_dir)
                for p, ms in found]
        self.sync_page.start_tool(tr(self.lang, "hl_task_many", n=len(found)), jobs, self._hilight_done)
        self.show_activity()
        return True
