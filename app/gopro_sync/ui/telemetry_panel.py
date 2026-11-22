"""Grafico de telemetria en el reproductor: velocidad (linea) y altura (area)
a lo largo del video, sincronizado con lo que se esta viendo. Clic o
arrastrar sobre el grafico = ir a ese momento. Muestra tambien los momentos
HiLight. Se abre con el boton del grafico o con la tecla G."""
import bisect
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QToolTip, QWidget

from ..i18n import tr
from ..gps_index import fast_track
from ..telemetry import chart_series
from ..workers import ScanThread

HILIGHT_COLOR = "#ffd166"
MARK_COLOR = "#ffffff"


def fmt_ms(ms):
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class TelemetryChart(QWidget):
    seek_requested = Signal(int)

    def __init__(self, accent, lang, parent=None):
        super().__init__(parent)
        self.accent = QColor(accent)
        self.lang = lang
        self.series = []          # [(segundo, km/h, m)]
        self._times = []
        self.duration = 0.0       # s
        self.position = 0         # ms
        self.hilights, self.marks = [], []
        self.loading = True
        self.setFixedHeight(132)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cache = None

    # ---- datos
    def set_loading(self):
        self.series, self._times, self.loading = [], [], True
        self._cache = None
        self.update()

    def set_data(self, series, duration):
        self.series, self.duration, self.loading = series or [], duration or 0.0, False
        self._times = [s[0] for s in self.series]
        self._cache = None
        self.update()

    def set_moments(self, hilights, marks):
        self.hilights, self.marks = list(hilights or []), list(marks or [])
        self._cache = None
        self.update()

    def set_position(self, ms):
        old_x = self._x(self.position / 1000.0)
        self.position = ms
        new_x = self._x(ms / 1000.0)
        if old_x is None or new_x is None or abs(new_x - old_x) >= 1:
            self.update()

    # ---- geometria
    def _plot(self):
        return QRectF(56, 26, max(10, self.width() - 112), self.height() - 44)

    def _x(self, sec):
        if not self.duration:
            return None
        r = self._plot()
        return r.left() + max(0.0, min(1.0, sec / self.duration)) * r.width()

    def _ms_at(self, x):
        r = self._plot()
        f = max(0.0, min(1.0, (x - r.left()) / max(1.0, r.width())))
        return int(f * self.duration * 1000)

    def _value_at(self, sec):
        if not self.series:
            return None
        i = max(0, min(len(self.series) - 1, bisect.bisect_left(self._times, sec)))
        return self.series[i][1], self.series[i][2]

    def resizeEvent(self, e):
        self._cache = None
        super().resizeEvent(e)

    # ---- dibujo
    def _ranges(self):
        vs = [s[1] for s in self.series]
        alts = [s[2] for s in self.series]
        vmax = max(5.0, max(vs) * 1.1) if vs else 5.0
        amin, amax = (min(alts), max(alts)) if alts else (0, 1)
        if amax - amin < 10:
            mid = (amax + amin) / 2
            amin, amax = mid - 5, mid + 5
        return vmax, amin, amax

    def _paint_static(self, p):
        r = self._plot()
        p.fillRect(self.rect(), QColor("#15151c"))
        f = QFont(self.font())
        f.setPixelSize(10)
        p.setFont(f)
        if self.loading or not self.series:
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       tr(self.lang, "chart_loading" if self.loading else "chart_no_gps"))
            self._paint_moments(p, r)
            return
        vmax, amin, amax = self._ranges()
        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        for k in range(1, 4):
            y = r.top() + r.height() * k / 4
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        # altura: area gris
        area = QPainterPath(QPointF(self._x(self.series[0][0]), r.bottom()))
        for t, _v, a in self.series:
            area.lineTo(QPointF(self._x(t), r.bottom() - (a - amin) / (amax - amin) * r.height()))
        area.lineTo(QPointF(self._x(self.series[-1][0]), r.bottom()))
        area.closeSubpath()
        g = QLinearGradient(0, r.top(), 0, r.bottom())
        g.setColorAt(0, QColor(255, 255, 255, 60))
        g.setColorAt(1, QColor(255, 255, 255, 8))
        p.fillPath(area, g)
        # velocidad: linea del color de la app
        line = QPainterPath()
        for i, (t, v, _a) in enumerate(self.series):
            pt = QPointF(self._x(t), r.bottom() - v / vmax * r.height())
            (line.moveTo if i == 0 else line.lineTo)(pt)
        p.setPen(QPen(self.accent, 2))
        p.drawPath(line)
        # escalas: velocidad a la izquierda, altura a la derecha
        p.setPen(self.accent)
        p.drawText(QRectF(0, r.top() - 6, 50, 14), Qt.AlignmentFlag.AlignRight, f"{vmax:.0f}")
        p.drawText(QRectF(0, r.bottom() - 8, 50, 14), Qt.AlignmentFlag.AlignRight, "0 km/h")
        p.setPen(QColor(255, 255, 255, 140))
        p.drawText(QRectF(r.right() + 6, r.top() - 6, 52, 14), Qt.AlignmentFlag.AlignLeft, f"{amax:.0f} m")
        p.drawText(QRectF(r.right() + 6, r.bottom() - 8, 52, 14), Qt.AlignmentFlag.AlignLeft, f"{amin:.0f} m")
        self._paint_moments(p, r)

    def _paint_moments(self, p, r):
        if not self.duration:
            return
        for ms, color in [(m, HILIGHT_COLOR) for m in self.hilights] + [(m, MARK_COLOR) for m in self.marks]:
            x = self._x(ms / 1000.0)
            p.setPen(QPen(QColor(color), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            tri = QPainterPath(QPointF(x - 5, r.top() - 10))
            tri.lineTo(x + 5, r.top() - 10)
            tri.lineTo(x, r.top() - 2)
            tri.closeSubpath()
            p.fillPath(tri, QColor(color))

    def paintEvent(self, _e):
        dpr = self.devicePixelRatioF()
        if self._cache is None or self._cache.size() != self.size() * dpr:
            self._cache = QPixmap(self.size() * dpr)
            self._cache.setDevicePixelRatio(dpr)
            cp = QPainter(self._cache)
            cp.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._paint_static(cp)
            cp.end()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = self._x(self.position / 1000.0)
        if x is not None:
            r = self._plot()
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.drawLine(QPointF(x, r.top() - 4), QPointF(x, r.bottom() + 2))
            val = self._value_at(self.position / 1000.0)
            if val:
                f = QFont(self.font())
                f.setPixelSize(12)
                f.setBold(True)
                p.setFont(f)
                p.setPen(QColor("#ffffff"))
                p.drawText(QRectF(r.left(), 4, r.width(), 16), Qt.AlignmentFlag.AlignLeft,
                           f"{val[0]:.0f} km/h  ·  {val[1]:.0f} m")
        p.end()

    # ---- mouse
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.duration:
            self.seek_requested.emit(self._ms_at(e.position().x()))

    def mouseMoveEvent(self, e):
        if not self.duration:
            return
        ms = self._ms_at(e.position().x())
        if e.buttons() & Qt.MouseButton.LeftButton:
            self.seek_requested.emit(ms)
        val = self._value_at(ms / 1000.0)
        tip = fmt_ms(ms) + (f"\n{val[0]:.0f} km/h · {val[1]:.0f} m" if val else "")
        QToolTip.showText(e.globalPosition().toPoint(), tip, self)


class TelemetryExtras(QObject):
    """Se engancha al VideoPlayerDialog: boton del grafico, el grafico debajo
    del video y la tecla G."""

    def __init__(self, player, controls_layout, main_layout, round_btn):
        super().__init__(player)
        self.pl = player
        self.lang = player.lang
        self.path = None
        self.gen = 0
        self.track = None
        accent = "#00f2fe"
        try:
            accent = player.parent().window().get_current_theme_color()
        except Exception:
            pass
        self.btn = round_btn("📈", tr(self.lang, "chart_toggle"))
        self.btn.setCheckable(True)
        self.btn.clicked.connect(self.toggle)
        idx = controls_layout.indexOf(player.btn_full)
        controls_layout.insertWidget(idx if idx >= 0 else controls_layout.count(), self.btn)
        self.chart = TelemetryChart(accent, self.lang, player)
        self.chart.seek_requested.connect(self.seek)
        self.chart.hide()
        pos = main_layout.indexOf(controls_layout.parentWidget())
        main_layout.insertWidget(pos if pos >= 0 else main_layout.count(), self.chart)
        player.player.sourceChanged.connect(self._on_source)
        player.player.positionChanged.connect(self._on_position)
        player.player.durationChanged.connect(lambda _d: self._fill())
        player.installEventFilter(self)

    def _on_source(self, url):
        path = self.pl._path if url is not None and not url.isEmpty() else ""
        if not path or path == self.path:
            return
        self.path = path
        self.gen += 1
        self.track = None
        self.chart.set_loading()
        th = ScanThread(self.gen, fast_track, path)
        th.done.connect(self._on_track)
        th.start()
        self._thread = th

    def _on_track(self, gen, track):
        if gen != self.gen:
            return
        self.track = track if track and track.get("points") else None
        self.chart.loading = False
        self._fill()

    def _fill(self):
        dur = self.pl.player.duration() / 1000.0
        if self.chart.loading or not dur:
            return
        self.chart.set_data(chart_series(self.track, dur) if self.track else [], dur)
        self.sync_moments()

    def sync_moments(self):
        """Los momentos HiLight y las marcas propias (los mismos de la barra)."""
        s = self.pl.slider_seek
        self.chart.set_moments(getattr(s, "hilights", ()), getattr(s, "user_marks", ()))

    def _on_position(self, pos):
        if self.chart.isVisible():
            self.chart.set_position(pos)

    def seek(self, ms):
        self.pl.slider_seek.setValue(ms)
        self.pl._do_seek(ms)
        self.chart.set_position(ms)

    def toggle(self):
        self.chart.setVisible(self.btn.isChecked())
        if self.chart.isVisible():
            self.sync_moments()
            self.chart.set_position(self.pl.player.position())

    def eventFilter(self, obj, event):
        if obj is self.pl and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_G:
            self.btn.setChecked(not self.btn.isChecked())
            self.toggle()
            return True
        return False


class TrackExportMixin:
    """Metodo de la ventana principal: guardar el recorrido GPS de un video
    como GPX (Strava, Garmin, Komoot...) o KML (Google Earth)."""

    def export_track(self, path, fmt, parent=None):
        import os
        from PySide6.QtCore import QStandardPaths
        from PySide6.QtWidgets import QFileDialog
        from ..env import save_config
        from ..telemetry import export_gpx, export_kml
        from ..winshell import show_in_explorer
        from ..ui.widgets import show_toast
        start = self.config.get("track_last_dir") or \
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        base = os.path.splitext(os.path.basename(path))[0]
        flt = "GPX (*.gpx)" if fmt == "gpx" else "KML (*.kml)"
        out, _f = QFileDialog.getSaveFileName(parent or self, tr(self.lang, "track_save_title"),
                                              os.path.join(start, f"{base}.{fmt}"), flt)
        if not out:
            return False
        if not out.lower().endswith("." + fmt):
            out += "." + fmt
        self.config["track_last_dir"] = os.path.dirname(out)
        save_config(self.config)
        where = parent or self
        func = export_gpx if fmt == "gpx" else export_kml

        def done(_t, ok):
            if ok:
                show_toast(where, tr(self.lang, "track_saved", name=os.path.basename(out)))
                show_in_explorer(out)
            else:
                show_toast(where, tr(self.lang, "track_failed"))
        if fmt == "gpx":
            th = ScanThread(0, func, path, out)
        else:
            th = ScanThread(0, lambda: func(path, out, labels=(tr(self.lang, "track_start"), tr(self.lang, "track_end"))))
        th.done.connect(done)
        self._track_export_thread = th
        th.start()
        return True

    def make_overlay(self, path, parent=None):
        """Video nuevo con velocidad, altura, distancia, hora y minimapa encima."""
        import os
        from ..env import save_config
        from ..media import read_mp4_info
        from ..overlay import overlay_job
        from ..videotools import share_dir
        from ..winshell import show_in_explorer
        from ..ui.feature_dialogs import OverlayDialog
        from ..ui.widgets import show_toast
        src_h = (read_mp4_info(path).get("video") or {}).get("h") or 1080
        start = self.config.get("ov_last_dir") or share_dir()
        if not os.path.isdir(start):
            start = share_dir()
        dlg = OverlayDialog(parent or self, self.lang, self.get_current_theme_color(), src_h, start,
                            self.config.get("ov_opts"))
        if not dlg.exec():
            return False
        opts = dlg.options()
        self.config["ov_opts"] = {k: v for k, v in opts.items() if k != "accent"}
        self.config["ov_last_dir"] = dlg.out_dir
        save_config(self.config)

        def done(ok, stats):
            outs = stats.get("outputs") or []
            if ok and outs:
                show_in_explorer(outs[0])
                target = os.path.normcase(os.path.abspath(self.sync_page.sync_target_dir))
                if os.path.normcase(os.path.abspath(outs[0])).startswith(target):
                    self.videos_page.refresh()
        self.sync_page.start_tool(tr(self.lang, "ov_task", name=os.path.basename(path)),
                                  [overlay_job(path, opts, self.lang, dlg.out_dir)], done)
        if parent is not None and parent is not self:
            show_toast(parent, tr(self.lang, "ov_started"))
        else:
            self.show_activity()
        return True
