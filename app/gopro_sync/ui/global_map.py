"""Mapa global: los recorridos y las fotos con GPS de toda la biblioteca, con
una lista (log) por dia. Los datos salen del indice GPS (gps_index), que se
llena en otro proceso: la ventana nunca lee la telemetria de los videos.

Rendimiento del dibujo (se usa el mismo motor de teselas y capas del mapa
del reproductor):
- Los recorridos se proyectan al cargar, en un hilo (calculo puro, sin Qt),
  y se guardan simplificados por nivel de zoom; solo se dibujan los que caen
  en la vista y los que a ese zoom miden menos de unos pixeles quedan como
  su marca.
- Cada tramo visible es una polilinea aparte (un unico camino enorme con
  cruces era 3 veces mas lento de rellenar) y las marcas son imagenes ya
  dibujadas que solo se pegan.
- La capa de recorridos se dibuja con margen alrededor de la vista: al
  arrastrar se reusa y solo se rehace al salir de ese margen o con otro zoom.
- Y se dibuja por partes (tandas de ~8 ms entre evento y evento) mientras se
  sigue viendo la anterior movida o ampliada, como en las apps de mapas: la
  ventana no se traba aunque haya cientos de recorridos en pantalla.
- Por zoom, cada recorrido se simplifica con Douglas-Peucker a 0,7 px: menos
  segmentos que dibujar sin diferencia visible.
- Las marcas se agrupan por celdas (con la cantidad) y el agrupamiento se
  guarda por zoom: al mover el mapa no se recalcula."""
import math
import os
import time
from collections import OrderedDict
from datetime import datetime
from PySide6.QtCore import QPointF, QProcess, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMenu, QProgressBar, QPushButton, QSplitter, QToolTip, QVBoxLayout, QWidget)
from ..env import FONT_STACK
from ..gps_index import decode_route, default_db_path, load_rows, progress_of, worker_command, worker_count
from ..i18n import tr
from ..sound import SoundManager
from ..workers import ScanThread
from .graphics import emoji_icon
from .map import TileMapWidget
from .widgets import show_toast

PHOTO_COLOR = "#ffb347"


def _n_videos(lang, n):
    return tr(lang, "gmap_one_video") if n == 1 else tr(lang, "gmap_n_videos", n=n)


def _n_photos(lang, n):
    return tr(lang, "gmap_one_photo") if n == 1 else tr(lang, "gmap_n_photos", n=n)


def _fmt_km(m):
    m = m or 0
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{m:.0f} m"


def _fmt_dur(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


class _Item:
    """Un video o una foto del indice (lo que hace falta para dibujarlo)."""
    __slots__ = ("key", "path", "name", "kind", "t", "lat", "lon", "bbox", "dist", "dur", "blob",
                 "wx", "wy", "wbox", "_route0", "day")

    def __init__(self, r):
        self.key = r["key"]
        self.path = r["path"]
        self.name = os.path.basename(self.path)
        self.kind = r["kind"]
        self.t = r["t"] or 0.0
        self.lat, self.lon = r["lat"], r["lon"]
        self.bbox = (r["minlat"], r["maxlat"], r["minlon"], r["maxlon"])
        self.dist, self.dur, self.blob = r["dist"], r["dur"], r["route"]
        w = TileMapWidget._world
        self.wx, self.wy = w(self.lat, self.lon, 0)
        x0, y0 = w(r["maxlat"], r["minlon"], 0)
        x1, y1 = w(r["minlat"], r["maxlon"], 0)
        self.wbox = (x0, y0, x1, y1)
        self._route0 = None
        try:
            self.day = datetime.fromtimestamp(self.t).date() if self.t else None
        except (OverflowError, OSError, ValueError):
            self.day = None

    def has_route(self):
        return self.kind == "v" and bool(self.blob)

    def route0(self):
        """Recorrido proyectado a zoom 0 (se calcula al cargar, en el hilo)."""
        if self._route0 is None:
            arr = decode_route(self.blob)
            w = TileMapWidget._world
            self._route0 = [w(arr[i] / 1e6, arr[i + 1] / 1e6, 0) for i in range(0, len(arr) - 1, 2)]
        return self._route0


def load_items(db_path, since_seq=0):
    """En un hilo: filas del indice -> elementos listos para dibujar (con el
    recorrido ya proyectado). No llama a Qt."""
    rows, last, meta = load_rows(db_path, since_seq)
    items = []
    for r in rows:
        try:
            it = _Item(r)
        except Exception:
            continue
        if it.has_route():
            it.route0()
        items.append(it)
    return items, last, meta


class GlobalMapWidget(TileMapWidget):
    item_clicked = Signal(object)        # _Item o None (clic en un lugar vacio)
    item_activated = Signal(object)      # doble clic sobre una marca o un recorrido
    CELL = 54                            # px del mundo por celda de agrupamiento
    LOD_CACHE = 800
    LAYER_MARGIN = 1.25                  # la capa de recorridos cubre algo mas que la vista
    JOB_BUDGET_S = 0.008                 # dibujo por partes: tiempo por tanda
    LOD_TOL_PX = 0.7                     # simplificacion por zoom (pixeles)
    FIT_MARGIN = 56                      # que los circulos de los grupos no queden cortados

    def __init__(self, accent, parent=None, lang="es"):
        super().__init__([], accent, parent, lang=lang)
        self.setMinimumSize(360, 300)
        self.items = []
        self.selected = None
        self._clusters = {}
        self._lod = OrderedDict()
        self._hits = None
        self._press = None
        self.zoom, self.scale = 2, 1.0
        self.cx = self.cy = 512.0               # el mundo entero mientras no hay nada
        self._sprites = {}
        self._route_gen = 0             # cambia con los datos, la seleccion o el color
        self._layer_gen = -1            # con que datos se dibujo la capa que se ve
        self._job = None                # (vista, generador, gen) del dibujo por partes en curso
        self._job_timer = QTimer(self)
        self._job_timer.setSingleShot(True)
        self._job_timer.setInterval(0)
        self._job_timer.timeout.connect(self._run_job)
        self.setMouseTracking(True)

    # ---- datos
    def set_items(self, items):
        self.items = items
        self._clusters.clear()
        self._lod.clear()
        if self.selected is not None and self.selected not in items:
            self.selected = None
        if items:
            self._bounds = (min(i.bbox[0] for i in items), max(i.bbox[1] for i in items),
                            min(i.bbox[2] for i in items), max(i.bbox[3] for i in items))
        else:
            self._bounds = None
        if not self._user_moved and self._bounds:
            self.fit()
        self.redraw_routes()

    def select(self, item):
        if item is not self.selected:
            self.selected = item
            self.redraw_routes()

    def redraw_routes(self):
        """Los datos cambiaron: se rehace la capa (la anterior se sigue
        viendo hasta que la nueva este lista)."""
        self._route_gen += 1
        self.update()

    def show_bounds(self, bounds, max_zoom=17):
        """Encuadra unos limites (lat_min, lat_max, lon_min, lon_max)."""
        old, self._bounds = self._bounds, bounds
        self.fit()
        self._bounds = old
        if self.zoom > max_zoom:
            f = 2.0 ** (max_zoom - self.zoom)
            self.cx, self.cy, self.zoom, self.scale = self.cx * f, self.cy * f, max_zoom, 1.0
        self._user_moved = True
        self.update()

    def show_item(self, item):
        self.show_bounds(item.bbox, 17 if item.has_route() else 16)

    # ---- dibujo
    @staticmethod
    def _dp(xs, ys, tol):
        """Douglas-Peucker sobre coordenadas en pixeles: indices que quedan."""
        n = len(xs)
        if n <= 2:
            return list(range(n))
        keep = bytearray(n)
        keep[0] = keep[-1] = 1
        tol2 = tol * tol
        stack = [(0, n - 1)]
        while stack:
            a, b = stack.pop()
            if b <= a + 1:
                continue
            ax, ay = xs[a], ys[a]
            dx, dy = xs[b] - ax, ys[b] - ay
            L2 = dx * dx + dy * dy
            best, bi = -1.0, -1
            for i in range(a + 1, b):
                px, py = xs[i] - ax, ys[i] - ay
                if L2 > 0:
                    cr = px * dy - py * dx
                    d2 = cr * cr / L2
                else:
                    d2 = px * px + py * py
                if d2 > best:
                    best, bi = d2, i
            if best > tol2:
                keep[bi] = 1
                stack.append((a, bi))
                stack.append((bi, b))
        return [i for i in range(n) if keep[i]]

    def _route_at(self, it, z):
        """Recorrido en pixeles del mundo al zoom z, simplificado para ese zoom
        (se calcula una vez por recorrido y zoom)."""
        key = (id(it), z)
        pts = self._lod.get(key)
        if pts is not None:
            self._lod.move_to_end(key)
            return pts
        f = float(1 << z)
        base = it.route0()
        xs = [b[0] * f for b in base]
        ys = [b[1] * f for b in base]
        pts = [(xs[i], ys[i]) for i in self._dp(xs, ys, self.LOD_TOL_PX)]
        self._lod[key] = pts
        while len(self._lod) > self.LOD_CACHE:
            self._lod.popitem(last=False)
        return pts

    @staticmethod
    def _runs(pts, x0, y0, x1, y1, left, top, out):
        """Tramos del recorrido que se ven (cada uno una polilinea)."""
        prev, prev_in, cur = None, False, None
        for x, y in pts:
            inside = x0 <= x <= x1 and y0 <= y <= y1
            if prev is not None and (inside or prev_in or
                                     not (max(x, prev[0]) < x0 or min(x, prev[0]) > x1 or
                                          max(y, prev[1]) < y0 or min(y, prev[1]) > y1)):
                if cur is None:
                    cur = [QPointF(prev[0] - left, prev[1] - top)]
                cur.append(QPointF(x - left, y - top))
            elif cur is not None:
                out.append(QPolygonF(cur))
                cur = None
            prev, prev_in = (x, y), inside
        if cur is not None:
            out.append(QPolygonF(cur))

    def _new_sprite(self, size_px, dpr):
        size = int(math.ceil(size_px * dpr))
        img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(QColor(0, 0, 0, 0))
        return img, size / dpr / 2

    def _sprite(self, kind, dpr):
        """Marca dibujada una sola vez: pegar una imagen es mucho mas rapido
        que dibujar cientos de circulos con borde y suavizado."""
        key = (kind, dpr, self.accent.name())
        img = self._sprites.get(key)
        if img is None:
            r = {"v": 7.0, "p": 5.5, "sel_v": 10.0, "sel_p": 10.0}[kind]
            img, c = self._new_sprite(2 * r + 8, dpr)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            fill = self.accent if kind in ("v", "sel_v") else QColor(PHOTO_COLOR)
            if kind.startswith("sel"):
                p.setPen(QPen(QColor(0, 0, 0, 160), 4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(c, c), r + 1, r + 1)
                p.setPen(QPen(QColor("#ffffff"), 3))
            else:
                p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(fill)
            p.drawEllipse(QPointF(c, c), r, r)
            p.end()
            self._sprites[key] = img
        return img

    def _cluster_sprite(self, r, videos, dpr):
        key = ("c", r, videos, dpr, self.accent.name())
        img = self._sprites.get(key)
        if img is None:
            img, c = self._new_sprite(2 * r + 8, dpr)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(self.accent if videos else QColor(PHOTO_COLOR), 3))
            p.setBrush(QColor(20, 22, 30, 225))
            p.drawEllipse(QPointF(c, c), r, r)
            p.end()
            self._sprites[key] = img
        return img

    def _route_layer_ok(self, view):
        """Sirve la capa ya dibujada mientras la vista quede adentro de ella
        (mismo zoom y mismos datos): al arrastrar no se redibuja."""
        return (self._route_layer is not None and self._layer_gen == self._route_gen
                and self._covers(self._route_layer[1], view))

    def _render_layers(self):
        view = self._view()
        if view[4] <= 0 or view[5] <= 0:
            return
        self._last_render = time.perf_counter()
        self._ensure_tiles_layer(view)
        if not self._route_layer_ok(view):
            job = self._job
            if job is None or job[2] != self._route_gen or not self._covers(job[0], view):
                z, s, cx, cy, w, h, dpr, layer = view
                ext = (z, s, cx, cy, int(w * self.LAYER_MARGIN), int(h * self.LAYER_MARGIN), dpr, layer)
                self._job = (ext, self._route_job(ext), self._route_gen)
                self._job_timer.start()
        self.update()

    def _run_job(self):
        job = self._job
        if job is None:
            return
        ext, gen_obj, gen = job
        t0 = time.perf_counter()
        try:
            while time.perf_counter() - t0 < self.JOB_BUDGET_S:
                next(gen_obj)
        except StopIteration as stop:
            img, hits = stop.value
            if self._job is job:
                self._job = None
            self._route_layer = (img, ext)
            self._hits = (ext, hits)
            self._layer_gen = gen
            self.update()
            return
        if self._job is job:
            self._job_timer.start()

    def render_now(self):
        """Dibuja la capa de una vez (para pruebas)."""
        view = self._view()
        z, s, cx, cy, w, h, dpr, layer = view
        ext = (z, s, cx, cy, int(w * self.LAYER_MARGIN), int(h * self.LAYER_MARGIN), dpr, layer)
        g = self._route_job(ext)
        try:
            while True:
                next(g)
        except StopIteration as stop:
            img, hits = stop.value
        self._job = None
        self._route_layer, self._hits, self._layer_gen = (img, ext), (ext, hits), self._route_gen
        return img

    def _clusters_job(self, z):
        """Agrupa las marcas por celdas a ese zoom (por partes; queda guardado)."""
        f, cell, grid = float(1 << z), self.CELL, {}
        for n, it in enumerate(self.items):
            x, y = it.wx * f, it.wy * f
            k = (int(x // cell), int(y // cell))
            g = grid.get(k)
            if g is None:
                grid[k] = [x, y, [it]]
            else:
                g[0] += x
                g[1] += y
                g[2].append(it)
            if n % 1500 == 1499:
                yield
        # segunda pasada: grupos de celdas vecinas con los centros muy cerca se
        # unen (si no, dos circulos quedaban uno encima del otro)
        cent = {k: (v[0] / len(v[2]), v[1] / len(v[2])) for k, v in grid.items()}
        lim2 = (cell * 0.9) ** 2
        used, cl = set(), []
        for k in sorted(grid, key=lambda c: -len(grid[c][2])):
            if k in used:
                continue
            used.add(k)
            sx, sy, its = grid[k][0], grid[k][1], list(grid[k][2])
            c0 = cent[k]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    k2 = (k[0] + dx, k[1] + dy)
                    if k2 in used or k2 not in grid:
                        continue
                    c2 = cent[k2]
                    if (c2[0] - c0[0]) ** 2 + (c2[1] - c0[1]) ** 2 < lim2:
                        used.add(k2)
                        g2 = grid[k2]
                        sx += g2[0]
                        sy += g2[1]
                        its.extend(g2[2])
            cl.append((sx / len(its), sy / len(its), its))
        self._clusters[z] = cl
        return cl

    def _route_job(self, view):
        """Dibuja recorridos y marcas en una imagen nueva, cediendo el control
        a la ventana cada pocos pasos. Devuelve (imagen, marcas para el clic)."""
        z, s, cx, cy, w, h, dpr, layer = view
        img = self._new_layer(view)
        hits = []
        items = self.items
        if not items:
            return img, hits
        f = float(1 << z)
        vw, vh = w / s, h / s
        left, top = cx - vw / 2, cy - vh / 2
        m = 30 / s
        x0, y0, x1, y1 = left - m, top - m, left + vw + m, top + vh + m
        selected = self.selected
        polys, sel_polys = [], []
        for it in items:
            if not it.has_route():
                continue
            bx0, by0, bx1, by1 = it.wbox
            if bx1 * f < x0 or bx0 * f > x1 or by1 * f < y0 or by0 * f > y1:
                continue
            if it is not selected and max((bx1 - bx0) * f, (by1 - by0) * f) < 3:
                continue                      # a este zoom es un punto: alcanza con la marca
            self._runs(self._route_at(it, z), x0, y0, x1, y1, left, top,
                       sel_polys if it is selected else polys)
            yield                             # un recorrido por paso (la primera vez se simplifica)
        clusters = self._clusters.get(z)
        if clusters is None:
            clusters = yield from self._clusters_job(z)
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            # uniones en angulo y puntas planas: la mitad de trabajo que redondeadas
            # (con 3 px de ancho no se nota)
            mj, fc, solid = Qt.PenJoinStyle.MiterJoin, Qt.PenCapStyle.FlatCap, Qt.PenStyle.SolidLine
            for group, pens in ((polys, ((QColor(0, 0, 0, 120), 5.5), (self.accent, 3.0))),
                                (sel_polys, ((QColor(0, 0, 0, 170), 9.0), (QColor("#ffffff"), 5.0)))):
                if not group:
                    continue
                for color, width in pens:
                    p.save()
                    p.scale(s, s)
                    p.setPen(QPen(color, width / s, solid, fc, mj))
                    for i, poly in enumerate(group):
                        p.drawPolyline(poly)
                        if i % 12 == 11:
                            p.restore()
                            yield
                            p.save()
                            p.scale(s, s)
                            p.setPen(QPen(color, width / s, solid, fc, mj))
                    p.restore()
            # marcas (agrupadas): imagenes ya dibujadas
            font = p.font()
            font.setBold(True)
            font.setPixelSize(11)
            p.setFont(font)
            p.setPen(QColor("#ffffff"))
            sel_xy = None
            spr = {"v": self._sprite("v", dpr), "p": self._sprite("p", dpr)}
            half = {k: v.width() / dpr / 2 for k, v in spr.items()}
            hit_r = {"v": 9, "p": 7.5}
            for n, (wx, wy, its) in enumerate(clusters):
                if n % 400 == 399:
                    yield
                if not (x0 <= wx <= x1 and y0 <= wy <= y1):
                    continue
                sx, sy = (wx - left) * s, (wy - top) * s
                if len(its) == 1:
                    it = its[0]
                    if it is selected:
                        sel_xy = (sx, sy, it.kind)
                        hits.append((sx, sy, 11, its))
                        continue
                    k = it.kind
                    p.drawImage(QPointF(sx - half[k], sy - half[k]), spr[k])
                    hits.append((sx, sy, hit_r[k], its))
                else:
                    cnt = len(its)
                    r = int(12 + 3.2 * math.log10(cnt))
                    csp = self._cluster_sprite(r, any(i.kind == "v" for i in its), dpr)
                    hc = csp.width() / dpr / 2
                    p.drawImage(QPointF(sx - hc, sy - hc), csp)
                    txt = str(cnt) if cnt < 10000 else f"{cnt // 1000}k"
                    p.drawText(QRectF(sx - r, sy - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, txt)
                    hits.append((sx, sy, r, its))
            if sel_xy is not None:
                ssp = self._sprite("sel_" + sel_xy[2], dpr)
                hs = ssp.width() / dpr / 2
                p.drawImage(QPointF(sel_xy[0] - hs, sel_xy[1] - hs), ssp)
        finally:
            p.end()
        return img, hits

    # ---- interaccion
    def _hit(self, pos):
        if not self._hits:
            return None
        lv, hits = self._hits
        k, ox, oy = self._layer_transform(lv, self._view())
        best, bd = None, 1e9
        for lx, ly, r, its in hits:
            d = math.hypot(pos.x() - (lx * k + ox), pos.y() - (ly * k + oy))
            if d <= r + 4 and d < bd:
                best, bd = its, d
        return best

    def _route_hit(self, pos):
        """Recorrido que pasa a menos de unos pixeles del punto."""
        vw, vh = self._view_wh()
        z, s = self.zoom, self.scale
        f = float(1 << z)
        px, py = self.cx - vw / 2 + pos.x() / s, self.cy - vh / 2 + pos.y() / s
        tol = 6 / s
        best, bd = None, tol * tol
        for it in self.items:
            if not it.has_route():
                continue
            bx0, by0, bx1, by1 = it.wbox
            if px < bx0 * f - tol or px > bx1 * f + tol or py < by0 * f - tol or py > by1 * f + tol:
                continue
            pts = self._route_at(it, z)
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
                qx, qy = ax + u * dx - px, ay + u * dy - py
                d2 = qx * qx + qy * qy
                if d2 < bd:
                    best, bd = it, d2
        return best

    def describe(self, its):
        if len(its) == 1:
            it = its[0]
            when = datetime.fromtimestamp(it.t).strftime("%d/%m/%Y %H:%M") if it.t else ""
            extra = f"  ·  {_fmt_km(it.dist)}  ·  {_fmt_dur(it.dur)}" if it.kind == "v" else ""
            return f"{it.name}\n{when}{extra}"
        nv = sum(1 for i in its if i.kind == "v")
        parts = []
        if nv:
            parts.append(_n_videos(self.lang, nv))
        if len(its) - nv:
            parts.append(_n_photos(self.lang, len(its) - nv))
        return "  ·  ".join(parts) + "\n" + tr(self.lang, "gmap_cluster_hint")

    def mousePressEvent(self, event):
        self._press = event.position() if event.button() == Qt.MouseButton.LeftButton else None
        QToolTip.hideText()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        press, self._press = self._press, None
        if press is not None and (event.position() - press).manhattanLength() < 5:
            self._click(event.position())

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self._drag:
            return
        its = self._hit(event.position())
        if its:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            QToolTip.showText(event.globalPosition().toPoint(), self.describe(its), self)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            QToolTip.hideText()

    def mouseDoubleClickEvent(self, event):
        its = self._hit(event.position())
        if its and len(its) == 1:
            self.item_activated.emit(its[0])
            return
        if not its:
            it = self._route_hit(event.position())
            if it is not None:
                self.item_activated.emit(it)
                return
        super().mouseDoubleClickEvent(event)

    def _click(self, pos):
        its = self._hit(pos)
        if its and len(its) == 1:
            self.select(its[0])
            self.item_clicked.emit(its[0])
            return
        if its:
            lat0, lat1 = min(i.lat for i in its), max(i.lat for i in its)
            lon0, lon1 = min(i.lon for i in its), max(i.lon for i in its)
            if self.zoom < 17 and (lat1 - lat0 > 1e-5 or lon1 - lon0 > 1e-5):
                old_zoom = self.zoom
                self.show_bounds((lat0, lat1, lon0, lon1), 18)
                if self.zoom <= old_zoom:            # no se separaban: acercar igual
                    self.zoom_at(2, pos)
                return
            self._cluster_menu(its, pos)
            return
        it = self._route_hit(pos)
        self.select(it)
        self.item_clicked.emit(it)

    def _cluster_menu(self, its, pos):
        """Varias marcas en el mismo lugar: elegir de una lista."""
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: #1f2230; color: #fff; border: 1px solid rgba(255,255,255,0.18);"
                           " border-radius: 8px; padding: 4px; }"
                           " QMenu::item { padding: 6px 18px 6px 12px; border-radius: 5px; }"
                           " QMenu::item:selected { background: rgba(255,255,255,0.12); }")
        for it in sorted(its, key=lambda i: i.t)[:40]:
            when = datetime.fromtimestamp(it.t).strftime("%d/%m/%Y %H:%M") if it.t else ""
            act = menu.addAction(emoji_icon("🎬" if it.kind == "v" else "📷", 16), f"{it.name}   {when}")
            act.triggered.connect(lambda _c=False, i=it: (self.select(i), self.item_clicked.emit(i)))
        menu.exec(self.mapToGlobal(pos.toPoint()))


class GlobalMapPage(QWidget):
    """Pagina "Mapa" del menu lateral: mapa global + log por dia."""
    POLL_MS = 1500

    def __init__(self, get_folder, get_accent, lang="es", parent=None):
        super().__init__(parent)
        self.get_folder, self.get_accent, self.lang = get_folder, get_accent, lang
        self.db = default_db_path()
        self.all = {}                 # ruta -> _Item
        self._seq = 0
        self._prune = None
        self._loaded = self._loading = self._polling = self._poll_again = False
        self._procs = []
        self._again = False
        self._indexing_meta = {}
        self._row_of = {}             # _Item -> QListWidgetItem
        self._years = []
        self._list_dirty = False

        self.setStyleSheet(f"""
            QWidget {{ font-family: {FONT_STACK}; }}
            QLabel {{ color: #fff; background: transparent; }}
            QPushButton {{ background: rgba(255,255,255,0.07); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 9px; padding: 7px 14px; font-size: 12px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.15); }}
            QPushButton:checked {{ background: rgba(255,255,255,0.22); border: 1px solid rgba(255,255,255,0.4); }}
            QComboBox {{ font-size: 12px; }}
            QListWidget {{ background: rgba(255,255,255,0.03); color: #fff; border: 1px solid rgba(255,255,255,0.08);
                          border-radius: 12px; padding: 6px; font-size: 12px; outline: none; }}
            QListWidget::item {{ padding: 5px 6px; border-radius: 6px; }}
            QListWidget::item:selected {{ background: rgba(255,255,255,0.14); color: #fff; }}
            QListWidget::item:hover {{ background: rgba(255,255,255,0.07); }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px 4px 0; }}
            QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.18); border-radius: 4px; min-height: 30px; }}
            QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.3); }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QProgressBar {{ background: rgba(255,255,255,0.08); border: none; border-radius: 3px; }}
            QProgressBar::chunk {{ border-radius: 3px; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.lbl_title = QLabel()
        self.lbl_title.setStyleSheet("font-size: 22px; font-weight: 800;")
        self.lbl_stats = QLabel()
        self.lbl_stats.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.6);")
        head.addWidget(self.lbl_title)
        head.addSpacing(8)
        head.addWidget(self.lbl_stats, 1)
        self.cmb_year = QComboBox()
        self.cmb_year.currentIndexChanged.connect(lambda _i: self._apply_filter())
        self.btn_videos = QPushButton()
        self.btn_photos = QPushButton()
        for b, ch in ((self.btn_videos, "🎬"), (self.btn_photos, "📷")):
            b.setCheckable(True)
            b.setChecked(True)
            b.setIcon(emoji_icon(ch, 16))
            b.clicked.connect(lambda _c=False: (SoundManager.play_click(), self._apply_filter()))
        self.btn_refresh = QPushButton()
        self.btn_refresh.setIcon(emoji_icon("🔄", 16))
        self.btn_refresh.clicked.connect(lambda: (SoundManager.play_click(), self.start_indexing()))
        for w in (self.cmb_year, self.btn_videos, self.btn_photos, self.btn_refresh):
            head.addWidget(w)
        lay.addLayout(head)

        prog = QHBoxLayout()
        self.lbl_progress = QLabel()
        self.lbl_progress.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.7);")
        self.bar = QProgressBar()
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        self.bar.setMaximumWidth(260)
        prog.addWidget(self.lbl_progress)
        prog.addWidget(self.bar)
        prog.addStretch()
        self.prog_row = QWidget()
        self.prog_row.setLayout(prog)
        prog.setContentsMargins(0, 0, 0, 0)
        self.prog_row.hide()
        lay.addWidget(self.prog_row)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setStyleSheet("QSplitter::handle { background: transparent; width: 10px; }")
        self.map = GlobalMapWidget(self.get_accent(), self, lang)
        self.map.item_clicked.connect(self._on_map_click)
        self.map.item_activated.connect(self.open_item)
        self.list = QListWidget()
        self.list.setMinimumWidth(260)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.itemClicked.connect(self._on_list_click)
        self.list.itemDoubleClicked.connect(self._on_list_double)
        self.split.addWidget(self.map)
        self.split.addWidget(self.list)
        self.split.setStretchFactor(0, 1)
        self.split.setSizes([900, 330])
        lay.addWidget(self.split, 1)

        # aviso en el medio del mapa (vacio, sin carpeta, primera lectura)
        self.note = QLabel(self.map)
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note.setWordWrap(True)
        self.note.setStyleSheet("background: rgba(20,22,30,0.85); color: #fff; border-radius: 12px;"
                                " padding: 14px 18px; font-size: 13px;")
        self.note.hide()
        # ficha del elegido, abajo a la izquierda del mapa
        self.card = QFrame(self.map)
        self.card.setObjectName("gmapCard")          # (QLabel tambien es un QFrame: sin nombre se pintaba igual)
        self.card.setStyleSheet("QFrame#gmapCard { background: rgba(20,22,30,0.92); border: 1px solid rgba(255,255,255,0.16);"
                                " border-radius: 12px; } QLabel { border: none; background: transparent; }")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(14, 10, 10, 10)
        cl.setSpacing(4)
        top = QHBoxLayout()
        self.card_title = QLabel()
        self.card_title.setStyleSheet("font-size: 13px; font-weight: 800;")
        btn_x = QPushButton("✕")
        btn_x.setFixedSize(26, 26)
        btn_x.setStyleSheet("QPushButton { background: transparent; border: none; color: rgba(255,255,255,0.6);"
                            " font-size: 13px; padding: 0; } QPushButton:hover { color: #fff; }")
        btn_x.clicked.connect(lambda: self._select(None))
        top.addWidget(self.card_title, 1)
        top.addWidget(btn_x)
        cl.addLayout(top)
        self.card_sub = QLabel()
        self.card_sub.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.7);")
        cl.addWidget(self.card_sub)
        self.card_open = QPushButton()
        self.card_open.clicked.connect(lambda: self.open_item(self.map.selected))
        row = QHBoxLayout()
        row.addWidget(self.card_open)
        row.addStretch()
        cl.addLayout(row)
        self.card.hide()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(self.POLL_MS)
        self.poll_timer.timeout.connect(self._poll)
        self.list_timer = QTimer(self)          # mientras llegan datos, la lista se rehace cada tanto
        self.list_timer.setSingleShot(True)
        self.list_timer.setInterval(2500)
        self.list_timer.timeout.connect(self._rebuild_list)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self.retranslate(lang)

    # ---- textos
    def retranslate(self, lang):
        self.lang = lang
        self.map.lang = lang
        self.lbl_title.setText(tr(lang, "gmap_title"))
        self.btn_videos.setText(tr(lang, "gmap_videos"))
        self.btn_photos.setText(tr(lang, "gmap_photos"))
        self.btn_refresh.setToolTip(tr(lang, "gmap_refresh"))
        self._fill_years()
        self._update_stats()
        self._update_progress()
        self._update_note()
        if self._loaded:
            self._rebuild_list()
        self._update_card()

    # ---- ciclo de vida
    def showEvent(self, event):
        super().showEvent(event)
        accent = self.get_accent()
        if accent and accent != self.map.accent.name():
            self.map.accent = QColor(accent)
            self.map._sprites.clear()
            self.map.redraw_routes()
        if not self._loaded and not self._loading:
            self._full_load()
        if self._loaded or self._loading:
            self.poll_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._procs:
            self.poll_timer.stop()

    def _full_load(self, index_after=True):
        self._index_after = index_after
        self._loading = True
        self._update_note()
        th = ScanThread(0, load_items, self.db, 0)
        th.done.connect(self._on_full_load)
        self._load_thread = th
        th.start()

    def _on_full_load(self, _t, result):
        self._loading = False
        rows, last, meta = result or ([], 0, {})
        self.all = {}
        self._seq = 0
        self._ingest(rows, last, meta)
        self._loaded = True
        self._prune = meta.get("prune", "0")
        self._rebuild_list()
        if getattr(self, "_index_after", True):
            self.start_indexing()

    def shutdown(self):
        self.poll_timer.stop()
        procs, self._procs = self._procs, []
        for proc in procs:
            try:
                proc.finished.disconnect()
            except Exception:
                pass
            proc.kill()
            proc.waitForFinished(2000)
        self.map.shutdown()

    # ---- indice en segundo plano
    def start_indexing(self):
        """Busca videos y fotos nuevos en la carpeta y lee su GPS (en otro
        proceso). Si ya esta corriendo, se repite al terminar."""
        folder = self.get_folder()
        if not folder or not os.path.isdir(folder):
            self._update_note()
            return
        if self._procs:
            self._again = True
            return
        n = worker_count()
        for shard in range(n):
            prog, args = worker_command(self.db, [folder], shard, n)
            proc = QProcess(self)
            proc.setProgram(prog)
            proc.setArguments(args)
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            proc.readyReadStandardOutput.connect(lambda p=proc: p.readAllStandardOutput())   # que no se llene el caño
            proc.finished.connect(lambda *_a, p=proc: self._on_worker_done(p))
            proc.errorOccurred.connect(lambda e, p=proc: self._on_worker_error(p, e))
            self._procs.append(proc)
        self._indexing_meta = {"state0": "scanning"}
        for proc in list(self._procs):
            proc.start()
        self.poll_timer.start()
        self._update_progress()

    def _on_worker_error(self, proc, err):
        print("[Mapa global] proceso del indice:", err)
        if err == QProcess.ProcessError.FailedToStart:
            self._on_worker_done(proc)

    def _on_worker_done(self, proc):
        # por identidad: comparar con == llama a Qt y el objeto puede estar ya
        # destruido (se cerro la ventana con el proceso andando)
        if not any(p is proc for p in self._procs):
            return
        self._procs = [p for p in self._procs if p is not proc]
        try:
            proc.deleteLater()
        except RuntimeError:
            pass
        if self._procs:
            return
        if self._again:
            self._again = False
            QTimer.singleShot(0, self.start_indexing)
            return
        self._indexing_meta = {}
        self._poll()                       # lo ultimo que quedo guardado
        if not self.isVisible():
            self.poll_timer.stop()

    def _poll(self):
        if self._polling:
            self._poll_again = True          # al terminar la consulta en curso, otra
            return
        if not self._loaded:
            self._update_progress()
            return
        self._polling = True
        th = ScanThread(0, load_items, self.db, self._seq)
        th.done.connect(self._on_poll)
        self._poll_thread = th
        th.start()

    def _on_poll(self, _t, result):
        self._polling = False
        again, self._poll_again = self._poll_again, False
        if not result:
            return
        rows, last, meta = result
        prune = meta.get("prune", "0")
        if self._prune is not None and prune != self._prune:
            # se borraron archivos de la carpeta: recargar todo (pocas veces)
            self._prune = prune
            self._loaded = False
            self._full_load(index_after=False)
            return
        self._prune = prune
        if self._procs:
            self._indexing_meta = meta
        self._ingest(rows, last, meta)
        if not self._procs and self.list_timer.isActive():
            self._rebuild_list()             # termino de leer: la lista al dia ya
        self._update_progress()
        if again:
            self._poll()

    def _ingest(self, rows, last, meta):
        for it in rows:
            self.all[it.key] = it
        self._seq = max(self._seq, last or 0)
        if rows:
            self._fill_years()
            self._apply_filter(rebuild_list=False)
            if not self._procs:
                self._rebuild_list()
            elif not self.list_timer.isActive():
                self.list_timer.start()      # mientras llegan datos, cada tanto

    # ---- filtros
    def _fill_years(self):
        years = sorted({it.day.year for it in self.all.values() if it.day}, reverse=True)
        if years == self._years and self.cmb_year.count():
            return
        cur = self.cmb_year.currentData()
        self._years = years
        self.cmb_year.blockSignals(True)
        self.cmb_year.clear()
        self.cmb_year.addItem(tr(self.lang, "gmap_all_years"), None)
        for y in years:
            self.cmb_year.addItem(str(y), y)
        idx = self.cmb_year.findData(cur)
        self.cmb_year.setCurrentIndex(max(0, idx))
        self.cmb_year.blockSignals(False)

    def _visible(self):
        year = self.cmb_year.currentData()
        kinds = {k for k, b in (("v", self.btn_videos), ("p", self.btn_photos)) if b.isChecked()}
        out = [it for it in self.all.values()
               if it.kind in kinds and (year is None or (it.day and it.day.year == year))]
        out.sort(key=lambda i: i.t)
        return out

    def _apply_filter(self, rebuild_list=True):
        items = self._visible()
        self.map.set_items(items)
        self._update_stats(items)
        self._update_note()
        self._update_card()
        if rebuild_list:
            self._rebuild_list()

    def _update_stats(self, items=None):
        items = self._visible() if items is None else items
        nv = sum(1 for i in items if i.kind == "v")
        km = sum(i.dist or 0 for i in items if i.kind == "v")
        np_ = len(items) - nv
        self.lbl_stats.setText(tr(self.lang, "gmap_stats", videos=nv, km=_fmt_km(km), photos=np_))

    def _update_progress(self):
        meta = self._indexing_meta if self._procs else {}
        if not meta:
            self.prog_row.hide()
            return
        state, done, total = progress_of(meta)
        if state == "indexing" and total:
            self.lbl_progress.setText(tr(self.lang, "gmap_indexing", done=done, total=total))
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.bar.show()
        elif state in ("indexing", "done") and not total:
            self.prog_row.hide()
            return
        else:
            self.lbl_progress.setText(tr(self.lang, "gmap_scanning"))
            self.bar.setRange(0, 0)          # barra en movimiento
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {self.get_accent()}; border-radius: 3px; }}")
        self.prog_row.show()
        self._update_note()

    def _update_note(self):
        folder = self.get_folder()
        text = ""
        if not folder or not os.path.isdir(folder):
            text = tr(self.lang, "gmap_no_folder")
        elif not self.all and (self._loading or self._procs):
            text = tr(self.lang, "gmap_first_hint")
        elif self._loaded and not self.all:
            text = tr(self.lang, "gmap_empty")
        if text:
            self.note.setText(text)
            self.note.setMaximumWidth(max(240, min(460, self.map.width() - 40)))
            self.note.adjustSize()
            self.note.move((self.map.width() - self.note.width()) // 2, (self.map.height() - self.note.height()) // 2)
            self.note.show()
            self.note.raise_()
        else:
            self.note.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._place_overlays)

    def _place_overlays(self):
        self._update_note()
        self.card.adjustSize()
        self.card.move(12, self.map.height() - self.card.height() - 30)

    # ---- lista (log por dia)
    def _rebuild_list(self):
        self.list_timer.stop()
        items = self.map.items
        sel = self.map.selected
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        self._row_of = {}
        days = OrderedDict()
        for it in sorted(items, key=lambda i: -i.t):
            days.setdefault(it.day, []).append(it)
        head_font = self.list.font()
        head_font.setBold(True)
        for day, its in days.items():
            vids = [i for i in its if i.kind == "v"]
            photos = [i for i in its if i.kind == "p"]
            parts = []
            if vids:
                parts.append(_n_videos(self.lang, len(vids)))
                parts.append(_fmt_km(sum(v.dist or 0 for v in vids)))
            if photos:
                parts.append(_n_photos(self.lang, len(photos)))
            label = day.strftime("%d/%m/%Y") if day else "—"
            h = QListWidgetItem(f"{label}   ·   " + "  ·  ".join(parts))
            h.setFlags(Qt.ItemFlag.ItemIsEnabled)
            h.setFont(head_font)
            h.setForeground(QColor(255, 255, 255, 150))
            h.setData(Qt.ItemDataRole.UserRole, ("day", its))
            self.list.addItem(h)
            for v in sorted(vids, key=lambda i: i.t):
                hh = datetime.fromtimestamp(v.t).strftime("%H:%M") if v.t else ""
                row = QListWidgetItem(emoji_icon("🎬", 16), f"{hh}   {v.name}   ·   {_fmt_km(v.dist)}   ·   {_fmt_dur(v.dur)}")
                row.setData(Qt.ItemDataRole.UserRole, ("item", v))
                row.setToolTip(v.path)
                self.list.addItem(row)
                self._row_of[v] = row
            if photos:
                if len(photos) == 1:
                    p = photos[0]
                    row = QListWidgetItem(emoji_icon("📷", 16), p.name)
                    row.setData(Qt.ItemDataRole.UserRole, ("item", p))
                    row.setToolTip(p.path)
                    self._row_of[p] = row
                else:
                    row = QListWidgetItem(emoji_icon("📷", 16), _n_photos(self.lang, len(photos)))
                    row.setData(Qt.ItemDataRole.UserRole, ("photos", photos))
                self.list.addItem(row)
        self.list.setUpdatesEnabled(True)
        if sel is not None and sel in self._row_of:
            self.list.setCurrentItem(self._row_of[sel])

    def _on_list_click(self, row):
        kind, data = row.data(Qt.ItemDataRole.UserRole) or (None, None)
        SoundManager.play_click()
        if kind == "item":
            self._select(data, from_list=True)
            self.map.show_item(data)
        elif kind in ("photos", "day"):
            self._select(None, from_list=True)
            self.map.show_bounds((min(i.bbox[0] for i in data), max(i.bbox[1] for i in data),
                                  min(i.bbox[2] for i in data), max(i.bbox[3] for i in data)))

    def _on_list_double(self, row):
        kind, data = row.data(Qt.ItemDataRole.UserRole) or (None, None)
        if kind == "item":
            self.open_item(data)
        elif kind == "photos" and data:
            self.open_item(min(data, key=lambda i: i.t))

    def _on_map_click(self, item):
        if item is not None:
            SoundManager.play_click()
        self._select(item)

    def _select(self, item, from_list=False):
        self.map.select(item)
        if not from_list:
            row = self._row_of.get(item)
            if row is not None:
                self.list.setCurrentItem(row)
                self.list.scrollToItem(row)
            else:
                self.list.clearSelection()
        self._update_card()

    def _update_card(self):
        it = self.map.selected
        if it is None:
            self.card.hide()
            return
        when = datetime.fromtimestamp(it.t).strftime("%d/%m/%Y  %H:%M") if it.t else ""
        self.card_title.setText(it.name)
        if it.kind == "v":
            self.card_sub.setText(f"{when}   ·   {_fmt_km(it.dist)}   ·   {_fmt_dur(it.dur)}")
            self.card_open.setText(tr(self.lang, "gmap_play"))
            self.card_open.setIcon(emoji_icon("▶", 14))
        else:
            self.card_sub.setText(f"{when}   ·   {it.lat:.5f}, {it.lon:.5f}")
            self.card_open.setText(tr(self.lang, "gmap_view_photo"))
            self.card_open.setIcon(emoji_icon("🖼", 14))
        self.card.show()
        self.card.raise_()
        self._place_overlays()

    # ---- abrir
    def open_item(self, it):
        if it is None:
            return
        if not os.path.exists(it.path):
            show_toast(self, tr(self.lang, "gmap_missing"))
            self.start_indexing()
            return
        SoundManager.play_click()
        if it.kind == "v":
            from .viewers import VideoPlayerDialog
            playlist = [i.path for i in self.map.items if i.kind == "v"]
            VideoPlayerDialog(it.path, gallery_dir=self.get_folder(), lang=self.lang, parent=self,
                              playlist=playlist).exec()
        else:
            from .viewers import ImageViewerDialog
            same_day = [i.path for i in self.map.items if i.kind == "p" and i.day == it.day]
            ImageViewerDialog(it.path, self, playlist=same_day, lang=self.lang).exec()
