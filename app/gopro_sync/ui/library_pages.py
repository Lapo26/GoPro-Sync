"""Paginas nuevas del menu lateral: mapa de viajes (todos los recorridos GPS)
y estadisticas de la biblioteca (espacio, horas, km, duplicados y archivos
.LRV / .THM que sobran)."""
import math
import os
from collections import defaultdict
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QMenu,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..i18n import _human_size, short_path, tr
from ..telemetry import track_summaries, video_summaries
from ..ui.map import TileMapWidget
from ..ui.telemetry_panel import MENU_STYLE

TRACK_COLORS = ["#4cc9f0", "#ffd166", "#06d6a0", "#ff6b9a", "#c77dff", "#ff9f1c", "#90be6d", "#f4f1de"]
_DAY_RE = __import__("re").compile(r"(\d{4})-(\d{2})-(\d{2})")

PAGE_BTN = ("QPushButton { background-color: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.14);"
            " border-radius: 10px; padding: 8px 16px; color: rgba(255,255,255,0.85); font-size: 13px; font-weight: 600; }"
            " QPushButton:hover { background-color: rgba(255,255,255,0.12); }"
            " QPushButton:disabled { color: rgba(255,255,255,0.35); }")
COMBO = ("QComboBox { background: rgba(255,255,255,0.07); color: #fff; border: 1px solid rgba(255,255,255,0.14);"
         " border-radius: 10px; padding: 7px 12px; min-width: 120px; }"
         " QComboBox QAbstractItemView { background: #23232b; color: #fff; selection-background-color: rgba(255,255,255,0.16); }")


def list_media(root, exts):
    out = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.name.lower().endswith(exts):
                            out.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def day_of(path):
    """Fecha del video: la de la carpeta (AAAA-MM-DD...) o la del archivo."""
    d = os.path.dirname(path)
    for _ in range(3):
        m = _DAY_RE.match(os.path.basename(d))
        if m:
            return m.group(0)
        d = os.path.dirname(d)
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except OSError:
        return "0000-00-00"


# ======================================================================= mapa de viajes
class TrackLoader(QThread):
    progress = Signal(int, int)
    done = Signal(object)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = root
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from ..media import gps_flags
        from ..env import CONFIG_DIR
        vids = list_media(self.root, (".mp4",))
        flags = gps_flags(vids, os.path.join(CONFIG_DIR, "gps_cache.json"))
        with_gps = [p for p in vids if flags.get(p)]
        out = {}
        for i, p in enumerate(with_gps, 1):
            if self._stop:
                return
            out.update(track_summaries([p]))
            self.progress.emit(i, len(with_gps))
        self.done.emit({p: v for p, v in out.items() if v and v.get("n")})


class TripsMap(TileMapWidget):
    """Mapa con muchos recorridos (uno por video), cada uno de un color.
    Clic sobre un recorrido = elegirlo."""
    track_clicked = Signal(str, QPointF)

    def __init__(self, tracks, accent, parent=None, lang="es"):
        self.tracks = tracks          # [(ruta, [[lat, lon], ...], color)]
        pts = [(q[0], q[1], 0.0, 0.0) for _p, ps, _c in tracks for q in ps] or [(20.0, 0.0, 0.0, 0.0)]
        super().__init__(pts, accent, parent, lang=lang)
        self.selected = None
        self._paths = {}
        self._press = None

    def _track_path(self, idx, z):
        key = (idx, z)
        path = self._paths.get(key)
        if path is None:
            path = QPainterPath()
            for j, q in enumerate(self.tracks[idx][1]):
                pt = QPointF(*self._world(q[0], q[1], z))
                (path.moveTo if j == 0 else path.lineTo)(pt)
            self._paths[key] = path
        return path

    def _paint_background(self, painter):
        keep, self.points = self.points, []
        try:
            super()._paint_background(painter)      # teselas y atribucion, sin el recorrido unico
        finally:
            self.points = keep
        if not self.tracks:
            return
        vw, vh = self._view_wh()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(self.scale, self.scale)
        painter.translate(-(self.cx - vw / 2), -(self.cy - vh / 2))
        s = self.scale
        painter.setBrush(Qt.BrushStyle.NoBrush)
        order = [i for i in range(len(self.tracks)) if self.tracks[i][0] != self.selected]
        order += [i for i in range(len(self.tracks)) if self.tracks[i][0] == self.selected]
        for i in order:
            path, _pts, color = self.tracks[i]
            sel = path == self.selected
            painter.setPen(QPen(QColor(0, 0, 0, 150), (8 if sel else 6) / s, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(self._track_path(i, self.zoom))
            painter.setPen(QPen(QColor("#ffffff" if sel else color), (5 if sel else 3) / s, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(self._track_path(i, self.zoom))
        painter.restore()

    def mousePressEvent(self, event):
        self._press = event.position()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._press is not None and (event.position() - self._press).manhattanLength() < 5:
            hit = self.track_at(event.position())
            if hit:
                self.track_clicked.emit(hit, event.globalPosition())
        self._press = None

    def track_at(self, pos, tol=10):
        best, best_d = None, tol
        for path, pts, _c in self.tracks:
            for q in pts:
                sp = self._to_screen(*self._world(q[0], q[1], self.zoom))
                d = math.hypot(sp.x() - pos.x(), sp.y() - pos.y())
                if d < best_d:
                    best, best_d = path, d
        return best

    def visible_bounds(self):
        """(lat_min, lat_max, lon_min, lon_max) de lo que se ve."""
        vw, vh = self._view_wh()
        n = 256 * (2 ** self.zoom)

        def latlon(wx, wy):
            lon = wx / n * 360.0 - 180.0
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy / n))))
            return lat, lon
        la1, lo0 = latlon(self.cx - vw / 2, self.cy - vh / 2)
        la0, lo1 = latlon(self.cx + vw / 2, self.cy + vh / 2)
        return la0, la1, lo0, lo1


class TripsMapPage(QWidget):
    """Todos los recorridos de la biblioteca en un mapa."""
    play_requested = Signal(str)
    filter_requested = Signal(object)      # set de rutas de video (o None = quitar filtro)

    def __init__(self, lang, accent, folder_getter, parent=None):
        super().__init__(parent)
        self.lang, self.accent, self.folder_getter = lang, accent, folder_getter
        self.data = None
        self.loader = None
        self.map = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(14)
        head = QHBoxLayout()
        self.title = QLabel(tr(lang, "trips_title"))
        self.title.setStyleSheet("font-size: 24px; font-weight: 800; color: #fff; background: transparent;")
        head.addWidget(self.title)
        head.addStretch()
        self.cmb_year = QComboBox()
        self.cmb_year.setStyleSheet(COMBO)
        self.cmb_year.currentIndexChanged.connect(lambda _i: self._build_map())
        head.addWidget(self.cmb_year)
        self.btn_zone = QPushButton(tr(lang, "trips_filter_zone"))
        self.btn_zone.setStyleSheet(PAGE_BTN)
        self.btn_zone.setToolTip(tr(lang, "trips_filter_zone_tip"))
        self.btn_zone.clicked.connect(self._filter_zone)
        head.addWidget(self.btn_zone)
        self.btn_reload = QPushButton(tr(lang, "trips_reload"))
        self.btn_reload.setStyleSheet(PAGE_BTN)
        self.btn_reload.clicked.connect(lambda: self.load(force=True))
        head.addWidget(self.btn_reload)
        outer.addLayout(head)
        self.lbl = QLabel("")
        self.lbl.setStyleSheet(f"font-size: 13px; color: {accent}; background: transparent;")
        outer.addWidget(self.lbl)
        self.slot = QVBoxLayout()
        outer.addLayout(self.slot, 1)
        self._loaded_for = None

    def showEvent(self, e):
        super().showEvent(e)
        if self._loaded_for != self.folder_getter():
            self.load()

    def load(self, force=False):
        if self.loader is not None and self.loader.isRunning():
            return
        self._loaded_for = self.folder_getter()
        self.lbl.setText(tr(self.lang, "trips_loading", i=0, n="…"))
        self.loader = TrackLoader(self._loaded_for, self)
        self.loader.progress.connect(lambda i, n: self.lbl.setText(tr(self.lang, "trips_loading", i=i, n=n)))
        self.loader.done.connect(self._on_loaded)
        self.loader.start()

    def _on_loaded(self, data):
        self.data = data or {}
        years = sorted({day_of(p)[:4] for p in self.data}, reverse=True)
        self.cmb_year.blockSignals(True)
        self.cmb_year.clear()
        self.cmb_year.addItem(tr(self.lang, "trips_all_years"), "")
        for y in years:
            self.cmb_year.addItem(y, y)
        self.cmb_year.blockSignals(False)
        self._build_map()

    def _visible_tracks(self):
        year = self.cmb_year.currentData() or ""
        items = sorted((p for p in self.data if not year or day_of(p).startswith(year)), key=day_of)
        return [(p, self.data[p]["pts"], TRACK_COLORS[i % len(TRACK_COLORS)]) for i, p in enumerate(items)]

    def _build_map(self):
        if self.data is None:
            return
        if self.map is not None:
            self.map.shutdown()
            self.map.setParent(None)
            self.map.deleteLater()
            self.map = None
        tracks = self._visible_tracks()
        km = sum(self.data[p].get("dist", 0) for p, _x, _c in tracks) / 1000
        days = len({day_of(p) for p, _x, _c in tracks})
        self.lbl.setText(tr(self.lang, "trips_summary", n=len(tracks), days=days, km=f"{km:.1f}") if tracks
                         else tr(self.lang, "trips_empty"))
        self.btn_zone.setEnabled(bool(tracks))
        if not tracks:
            return
        self.map = TripsMap(tracks, self.accent, self, lang=self.lang)
        self.map.track_clicked.connect(self._on_track)
        self.slot.addWidget(self.map)

    def _on_track(self, path, gpos):
        self.map.selected = path
        self.map._invalidate()
        info = self.data.get(path) or {}
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        dist = info.get("dist", 0)
        head = menu.addAction(f"{os.path.basename(path)}  ·  {day_of(path)}  ·  "
                              + (f"{dist / 1000:.1f} km" if dist >= 1000 else f"{dist:.0f} m"))
        head.setEnabled(False)
        menu.addAction(tr(self.lang, "trips_play"), lambda: self.play_requested.emit(path))
        menu.addAction(tr(self.lang, "ctx_map"), lambda: self._open_map(path))
        menu.exec(gpos.toPoint())

    def _open_map(self, path):
        from ..ui.map import GpsMapDialog
        GpsMapDialog(self, self.lang, path, self.accent).exec()

    def _filter_zone(self):
        if self.map is None:
            return
        la0, la1, lo0, lo1 = self.map.visible_bounds()
        inside = {p for p, pts, _c in self.map.tracks
                  if any(la0 <= q[0] <= la1 and lo0 <= q[1] <= lo1 for q in pts)}
        self.filter_requested.emit(inside)

    def retranslate(self, lang):
        self.lang = lang
        self.title.setText(tr(lang, "trips_title"))
        self.btn_zone.setText(tr(lang, "trips_filter_zone"))
        self.btn_reload.setText(tr(lang, "trips_reload"))


# ======================================================================= estadisticas
def _proxy_owner(path):
    """Video al que pertenece un .LRV / .THM (si esta en la misma carpeta)."""
    d, name = os.path.split(path)
    base = os.path.splitext(name)[0].upper()
    cands = [base]
    if base.startswith("GL"):
        cands = ["GX" + base[2:], "GH" + base[2:]]
    return [os.path.join(d, c + ext) for c in cands for ext in (".MP4", ".mp4")]


class StatsLoader(QThread):
    progress = Signal(str)
    done = Signal(object)

    def __init__(self, root, lang, parent=None):
        super().__init__(parent)
        self.root, self.lang = root, lang

    def run(self):
        files = list_media(self.root, (".mp4", ".jpg", ".jpeg", ".png", ".gpr", ".lrv", ".thm"))
        by_year = defaultdict(int)
        by_month = defaultdict(int)
        counts = defaultdict(int)
        sizes = {}
        proxies, orphans = [], []
        seen = defaultdict(list)
        videos = []
        for p in files:
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            sizes[p] = sz
            ext = os.path.splitext(p)[1].lower()
            day = day_of(p)
            if ext in (".lrv", ".thm"):
                proxies.append(p)
                if not any(os.path.exists(c) for c in _proxy_owner(p)):
                    orphans.append(p)
                counts["proxy"] += 1
                continue
            kind = "video" if ext == ".mp4" else "photo"
            counts[kind] += 1
            by_year[day[:4]] += sz
            by_month[day[:7]] += sz
            seen[(os.path.basename(p).lower(), sz)].append(p)
            if kind == "video":
                videos.append(p)
        dups = []
        for (_n, sz), paths in seen.items():
            if len(paths) > 1:
                paths = sorted(paths, key=lambda x: (len(x), x))
                dups.append((paths[0], paths[1:], sz))
        self.progress.emit(tr(self.lang, "stats_reading_videos", n=len(videos)))
        summ = video_summaries(videos)
        hours = sum((v or {}).get("d", 0) for v in summ.values() if isinstance(v, dict)) / 3600
        res = defaultdict(int)
        for v in summ.values():
            if isinstance(v, dict) and v.get("h"):
                h = v["h"]
                res["4K" if h >= 2000 else "2.7K" if h >= 1440 else "1440p" if h >= 1400 else
                    "1080p" if h >= 1000 else "720p" if h >= 700 else "SD"] += 1
        data = {"by_year": dict(by_year), "by_month": dict(by_month), "counts": dict(counts),
                "total": sum(sz for p, sz in sizes.items() if not p.lower().endswith((".lrv", ".thm"))),
                "proxies": proxies, "orphans": orphans, "proxy_size": sum(sizes.get(p, 0) for p in proxies),
                "orphan_size": sum(sizes.get(p, 0) for p in orphans), "dups": dups,
                "dup_size": sum(sz * len(extra) for _k, extra, sz in dups), "hours": hours, "res": dict(res),
                "videos": videos, "km": None}
        self.done.emit(data)
        # kilometros: solo de lo que ya esta leido (el mapa de viajes lee el resto)
        from ..telemetry import TRACK_CACHE
        TRACK_CACHE.get_many([], lambda p: None)
        km = 0.0
        known = 0
        with TRACK_CACHE.lock:
            cache = dict(TRACK_CACHE.data or {})
        for p in videos:
            try:
                st = os.stat(p)
            except OSError:
                continue
            v = cache.get(f"{os.path.normcase(p)}|{st.st_size}|{int(st.st_mtime)}")
            if isinstance(v, dict):
                known += 1
                km += v.get("dist", 0) / 1000
        data = dict(data, km=km, km_known=known)
        self.done.emit(data)


class BarChart(QWidget):
    """Barras simples (espacio por ano o por mes)."""

    def __init__(self, accent, parent=None):
        super().__init__(parent)
        self.accent = QColor(accent)
        self.items = []
        self.setMinimumHeight(200)

    def set_items(self, items):
        self.items = items
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.items:
            return
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        top, bottom = 22, self.height() - 22
        n = len(self.items)
        slot = self.width() / n
        vmax = max(v for _l, v in self.items) or 1
        for i, (label, v) in enumerate(self.items):
            h = (bottom - top) * v / vmax
            w = min(46.0, slot * 0.7)
            x = i * slot + (slot - w) / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.accent)
            p.drawRoundedRect(QRectF(x, bottom - h, w, max(2.0, h)), 4, 4)
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(i * slot, bottom + 4, slot, 16), Qt.AlignmentFlag.AlignCenter, label)
            if slot > 44:
                p.drawText(QRectF(i * slot, bottom - h - 18, slot, 16), Qt.AlignmentFlag.AlignCenter,
                           _human_size(v).replace(".0 ", " "))
        p.end()


class LibraryStatsPage(QWidget):
    """Estadisticas de la biblioteca y limpieza de lo que sobra."""

    def __init__(self, lang, accent, folder_getter, parent=None):
        super().__init__(parent)
        self.lang, self.accent, self.folder_getter = lang, accent, folder_getter
        self.data = None
        self.loader = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(14)
        head = QHBoxLayout()
        self.title = QLabel(tr(lang, "stats_title"))
        self.title.setStyleSheet("font-size: 24px; font-weight: 800; color: #fff; background: transparent;")
        head.addWidget(self.title)
        head.addStretch()
        self.btn_reload = QPushButton(tr(lang, "trips_reload"))
        self.btn_reload.setStyleSheet(PAGE_BTN)
        self.btn_reload.clicked.connect(self.load)
        head.addWidget(self.btn_reload)
        outer.addLayout(head)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"font-size: 13px; color: {accent}; background: transparent;")
        outer.addWidget(self.lbl_status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(16)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self.cards = QGridLayout()
        self.cards.setSpacing(12)
        self.body.addLayout(self.cards)
        self._card_labels = {}
        for i, key in enumerate(("stats_total", "stats_videos", "stats_photos", "stats_hours", "stats_km", "stats_res")):
            card = QFrame()
            card.setStyleSheet("QFrame { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);"
                               " border-radius: 14px; } QLabel { border: none; background: transparent; }")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 12, 16, 12)
            cap = QLabel(tr(lang, key))
            cap.setStyleSheet("font-size: 11px; font-weight: 700; color: rgba(255,255,255,0.55); letter-spacing: 1px;")
            val = QLabel("…")
            val.setWordWrap(True)
            val.setStyleSheet("font-size: 20px; font-weight: 800; color: #fff;")
            cl.addWidget(cap)
            cl.addWidget(val)
            self._card_labels[key] = (cap, val)
            self.cards.addWidget(card, i // 3, i % 3)

        row = QHBoxLayout()
        self.lbl_chart = QLabel(tr(lang, "stats_by_year"))
        self.lbl_chart.setStyleSheet("font-size: 15px; font-weight: 800; color: #fff; background: transparent;")
        row.addWidget(self.lbl_chart)
        row.addStretch()
        self.cmb_chart = QComboBox()
        self.cmb_chart.setStyleSheet(COMBO)
        self.cmb_chart.currentIndexChanged.connect(lambda _i: self._fill_chart())
        row.addWidget(self.cmb_chart)
        self.body.addLayout(row)
        self.chart = BarChart(accent)
        self.body.addWidget(self.chart)

        self.lbl_clean = QLabel(tr(lang, "stats_cleanup"))
        self.lbl_clean.setStyleSheet("font-size: 15px; font-weight: 800; color: #fff; background: transparent;")
        self.body.addWidget(self.lbl_clean)
        self.clean_rows = QVBoxLayout()
        self.clean_rows.setSpacing(8)
        self.body.addLayout(self.clean_rows)
        self.body.addStretch()
        self._loaded_for = None

    def showEvent(self, e):
        super().showEvent(e)
        if self._loaded_for != self.folder_getter():
            self.load()

    def load(self):
        if self.loader is not None and self.loader.isRunning():
            return
        self._loaded_for = self.folder_getter()
        self.lbl_status.setText(tr(self.lang, "stats_loading"))
        self.loader = StatsLoader(self._loaded_for, self.lang, self)
        self.loader.progress.connect(self.lbl_status.setText)
        self.loader.done.connect(self._on_data)
        self.loader.start()

    def _set(self, key, text):
        self._card_labels[key][1].setText(text)

    def _on_data(self, d):
        self.data = d
        c = d["counts"]
        self.lbl_status.setText(tr(self.lang, "stats_folder", path=short_path(self._loaded_for, keep=3)))
        self._set("stats_total", _human_size(d["total"]))
        self._set("stats_videos", str(c.get("video", 0)))
        self._set("stats_photos", str(c.get("photo", 0)))
        h = d["hours"]
        self._set("stats_hours", f"{int(h)} h {int(h * 60) % 60:02d} min")
        if d.get("km") is None:
            self._set("stats_km", "…")
        else:
            txt = f"{d['km']:.1f} km"
            if d.get("km_known", 0) < len(d["videos"]):
                txt += "\n" + tr(self.lang, "stats_km_partial")
            self._set("stats_km", txt)
        res = sorted(d["res"].items(), key=lambda kv: -kv[1])[:3]
        self._set("stats_res", "  ·  ".join(f"{k} {v}" for k, v in res) or "—")
        years = sorted(d["by_year"], reverse=True)
        cur = self.cmb_chart.currentData()
        self.cmb_chart.blockSignals(True)
        self.cmb_chart.clear()
        self.cmb_chart.addItem(tr(self.lang, "stats_by_year"), "")
        for y in years:
            self.cmb_chart.addItem(tr(self.lang, "stats_months_of", y=y), y)
        i = self.cmb_chart.findData(cur)
        self.cmb_chart.setCurrentIndex(max(0, i))
        self.cmb_chart.blockSignals(False)
        self._fill_chart()
        self._fill_cleanup()

    def _fill_chart(self):
        if not self.data:
            return
        y = self.cmb_chart.currentData()
        if not y:
            items = sorted(self.data["by_year"].items())
            self.lbl_chart.setText(tr(self.lang, "stats_by_year"))
        else:
            items = [(f"{m:02d}", self.data["by_month"].get(f"{y}-{m:02d}", 0)) for m in range(1, 13)]
            self.lbl_chart.setText(tr(self.lang, "stats_months_of", y=y))
        self.chart.set_items(items)

    def _fill_cleanup(self):
        while self.clean_rows.count():
            it = self.clean_rows.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        d = self.data
        rows = []
        if d["dups"]:
            rows.append((tr(self.lang, "stats_dups", n=sum(len(e) for _k, e, _s in d["dups"]), size=_human_size(d["dup_size"])),
                         tr(self.lang, "stats_review"), self._review_dups))
        if d["orphans"]:
            rows.append((tr(self.lang, "stats_orphans", n=len(d["orphans"]), size=_human_size(d["orphan_size"])),
                         tr(self.lang, "stats_recycle"), lambda: self._recycle(d["orphans"], "stats_orphans_q")))
        if d["proxies"]:
            rows.append((tr(self.lang, "stats_proxies", n=len(d["proxies"]), size=_human_size(d["proxy_size"])),
                         tr(self.lang, "stats_recycle"), lambda: self._recycle(d["proxies"], "stats_proxies_q")))
        if not rows:
            lbl = QLabel(tr(self.lang, "stats_clean_ok"))
            lbl.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.65); background: transparent;")
            self.clean_rows.addWidget(lbl)
            return
        for text, btn_text, fn in rows:
            fr = QFrame()
            fr.setStyleSheet("QFrame { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);"
                             " border-radius: 12px; } QLabel { border: none; background: transparent; color: #fff; }")
            h = QHBoxLayout(fr)
            h.setContentsMargins(16, 10, 10, 10)
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size: 13px;")
            b = QPushButton(btn_text)
            b.setStyleSheet(PAGE_BTN)
            b.clicked.connect(fn)
            h.addWidget(lbl, 1)
            h.addWidget(b)
            self.clean_rows.addWidget(fr)

    def _confirm(self, question, items):
        from ..ui.feature_dialogs import dialog_style
        dlg = QDialog(self)
        dlg.setWindowTitle(tr(self.lang, "stats_cleanup"))
        dlg.setMinimumWidth(620)
        dlg.setStyleSheet(dialog_style(self.accent) + " QListWidget { background: rgba(0,0,0,0.25); color: #ddd;"
                          " border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; font-size: 11px; }")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 18, 22, 16)
        q = QLabel(question)
        q.setWordWrap(True)
        q.setStyleSheet("font-size: 13px;")
        lay.addWidget(q)
        lst = QListWidget()
        for x in items[:2000]:
            lst.addItem(os.path.normpath(x))
        lay.addWidget(lst, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton(tr(self.lang, "cancel_btn"))
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton(tr(self.lang, "stats_recycle"))
        ok.setObjectName("primary")
        ok.clicked.connect(dlg.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        dlg.resize(700, 480)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _recycle(self, paths, qkey):
        from ..winshell import move_to_recycle_bin
        from ..ui.widgets import show_toast
        size = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        if not self._confirm(tr(self.lang, qkey, n=len(paths), size=_human_size(size)), paths):
            return
        ok = move_to_recycle_bin(list(paths))
        show_toast(self, tr(self.lang, "stats_recycled", n=len(paths)) if ok else tr(self.lang, "delete_failed",
                                                                                   name=os.path.basename(paths[0])))
        self.load()

    def _review_dups(self):
        extra = [x for _keep, e, _s in self.data["dups"] for x in e]
        lines = []
        for keep, e, _s in self.data["dups"]:
            lines.append("✓ " + os.path.normpath(keep))
            lines += ["   ✗ " + os.path.normpath(x) for x in e]
        size = self.data["dup_size"]
        if not self._confirm(tr(self.lang, "stats_dups_q", n=len(extra), size=_human_size(size)), lines):
            return
        from ..winshell import move_to_recycle_bin
        from ..ui.widgets import show_toast
        ok = move_to_recycle_bin(extra)
        show_toast(self, tr(self.lang, "stats_recycled", n=len(extra)) if ok else tr(self.lang, "delete_failed",
                                                                                   name=os.path.basename(extra[0])))
        self.load()

    def retranslate(self, lang):
        self.lang = lang
        self.title.setText(tr(lang, "stats_title"))
        self.btn_reload.setText(tr(lang, "trips_reload"))
        for key, (cap, _v) in self._card_labels.items():
            cap.setText(tr(lang, key))
        self.lbl_clean.setText(tr(lang, "stats_cleanup"))
        if self.data:
            self._on_data(self.data)
