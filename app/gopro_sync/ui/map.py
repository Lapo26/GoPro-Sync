"""Mapa con el recorrido GPS (callejero, satelite, topografico...)."""
import os
import tempfile
import time
from PySide6.QtCore import QPoint, QPointF, QRectF, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget
from ..env import FONT_STACK
from ..i18n import tr
from ..media import read_gopro_track, read_mp4_info
from ..workers import ScanThread
from ..updates import _http_get

# Mapas de fondo gratuitos, sin clave y de todo el mundo. {s} = subdominio para repartir las
# descargas; "max" = ultimo zoom que tiene el servidor (mas alla se amplia
# la tesela de ese nivel); "detail" = niveles de zoom de mas que se piden
# (1 = teselas de la mitad de tamano: el doble de resolucion). Solo en las
# fotos aereas: en un callejero achicaria los nombres de las calles.
# (CARTO pide clave desde 2026: sus teselas solo dicen "API KEY REQUIRED".)
MAP_LAYERS = {
    "satellite": {
        "label": "map_layer_satellite",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "subs": "", "ext": "jpg", "max": 19, "detail": 1,
        "attrib": "© Esri · Maxar · Earthstar Geographics",
    },
    "topo": {
        "label": "map_layer_topo",
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "subs": "abc", "ext": "png", "max": 17,
        "attrib": "© OpenStreetMap contributors · SRTM · © OpenTopoMap (CC-BY-SA)",
    },
    "osm": {
        "label": "map_layer_osm",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "subs": "", "ext": "png", "max": 19,
        "attrib": "© OpenStreetMap contributors",
    },
}
DEFAULT_LAYER = "satellite"


class TileMapWidget(QWidget):
    """Mapa con el recorrido: arrastrar para mover, rueda, doble clic o
    botones para acercar; un boton elige el mapa de fondo (callejero,
    satelite...). Las teselas se guardan en disco para no volver a pedirlas.
    En el reproductor muestra ademas la telemetria del punto que se esta
    viendo (velocidad, altura, distancia y tiempo)."""
    tile_loaded = Signal(str)
    FIT_MARGIN = 28                 # px libres alrededor del recorrido al encuadrar

    def __init__(self, points, accent, parent=None, elapsed=None, lang="es"):
        super().__init__(parent)
        from concurrent.futures import ThreadPoolExecutor
        self.points = points
        self.accent = QColor(accent)
        self.lang = lang
        self.elapsed = elapsed or 0     # segundos reales grabados (para el reloj de la telemetria)
        self.tiles = {}
        self.pending = set()
        self._failed = {}               # tesela -> momento del ultimo fallo
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.cache_dir = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "GoProSyncPro", "tiles")
        self.layer = self._load_layer()
        # Vista: zoom entero de las teselas + escala 1..2 encima, para que el
        # recorrido llene el panel (con zoom entero quedaba a veces a la mitad).
        self.zoom = 3
        self.scale = 1.0
        self.cx = self.cy = 0.0         # centro, en pixeles del mundo al zoom entero
        self._drag = None
        self.setMinimumSize(640, 420)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.tile_loaded.connect(lambda _k: self._invalidate())
        self._fitted = False
        self._user_moved = False
        self.marker = None              # indice del punto actual (reproductor)
        # Fondo ya dibujado (teselas + recorrido): mientras se reproduce el
        # video solo se mueven el punto blanco y la telemetria encima.
        self._bg = None
        self._bg_key = None
        self._routes = {}               # zoom -> recorrido en coordenadas del mundo
        self._cum = self._cumulative_distance()
        # Botones de zoom sobre el mapa
        self._zoom_btns = []
        for text, fn in (("+", lambda: self.zoom_at(1)), ("−", lambda: self.zoom_at(-1)), ("⤢", self.refit)):
            b = QToolButton(self)
            b.setText(text)
            b.setFixedSize(34, 34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet("QToolButton { background: rgba(20,22,30,0.85); color: #fff; border: 1px solid rgba(255,255,255,0.25);"
                            " border-radius: 8px; font-size: 18px; font-weight: 700; }"
                            " QToolButton:hover { background: rgba(40,44,60,0.95); }")
            b.clicked.connect(fn)
            self._zoom_btns.append(b)
        # Boton para elegir el mapa de fondo, debajo de los de zoom
        self._layer_btn = QToolButton(self)
        self._layer_btn.setText("🗺")
        self._layer_btn.setToolTip(tr(lang, "map_layer_choose"))
        self._layer_btn.setFixedSize(34, 34)
        self._layer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._layer_btn.setStyleSheet(self._zoom_btns[0].styleSheet().replace("font-size: 18px", "font-size: 16px"))
        self._layer_btn.clicked.connect(self._show_layer_menu)
        self._zoom_btns.append(self._layer_btn)

    # ---- coordenadas
    @staticmethod
    def _world(lat, lon, z):
        import math
        n = 256 * (2 ** z)
        lat = max(-85.0511, min(85.0511, lat))
        x = (lon + 180.0) / 360.0 * n
        y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
        return x, y

    def _view_wh(self):
        return self.width() / self.scale, self.height() / self.scale

    def _to_screen(self, wx, wy):
        vw, vh = self._view_wh()
        return QPointF((wx - (self.cx - vw / 2)) * self.scale, (wy - (self.cy - vh / 2)) * self.scale)

    def _point_screen(self, idx):
        pnt = self.points[idx]
        return self._to_screen(*self._world(pnt[0], pnt[1], self.zoom))

    def _cumulative_distance(self):
        """Distancia recorrida hasta cada punto. Solo suma cuando el punto se
        alejo 3 m del anterior tenido en cuenta, para que el temblor del GPS
        con la camara quieta no sume metros."""
        from ..media import _haversine
        cum, total, anchor = [], 0.0, None
        for p in self.points:
            if anchor is None:
                anchor = p
            else:
                d = _haversine(anchor, p)
                if d >= 3:
                    total += d
                    anchor = p
            cum.append(total)
        return cum

    def fit(self):
        if not self.points:
            return
        lats = [p[0] for p in self.points]
        lons = [p[1] for p in self.points]
        w = max(100, self.width() - 2 * self.FIT_MARGIN)
        h = max(100, self.height() - 2 * self.FIT_MARGIN)
        z = 18
        while z > 2:
            x1, y1 = self._world(max(lats), min(lons), z)
            x2, y2 = self._world(min(lats), max(lons), z)
            if abs(x2 - x1) <= w and abs(y2 - y1) <= h:
                break
            z -= 1
        x1, y1 = self._world(max(lats), min(lons), z)
        x2, y2 = self._world(min(lats), max(lons), z)
        bw, bh = max(1.0, abs(x2 - x1)), max(1.0, abs(y2 - y1))
        self.zoom = z
        # agranda hasta que el recorrido toque el margen (menos de x2: si no,
        # habria entrado al zoom siguiente)
        self.scale = max(1.0, min(1.99, w / bw, h / bh))
        if z == 18:
            self.scale = min(self.scale, 1.5)      # recorrido de pocos metros: no acercar de mas
        self.cx, self.cy = (x1 + x2) / 2, (y1 + y2) / 2

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Encuadra el recorrido cada vez que cambia el tamano (en el reproductor
        # el panel nace chico y despues se agranda), hasta que el usuario lo mueve.
        if not self._user_moved:
            self._fitted = True
            self.fit()
        self._bg = None
        for i, b in enumerate(self._zoom_btns):
            b.move(self.width() - 44, 10 + i * 40)

    # ---- teselas
    def _invalidate(self):
        self._bg = None
        self.update()

    def _route(self, z):
        path = self._routes.get(z)
        if path is None:
            path = QPainterPath()
            step = max(1, len(self.points) // 3000)
            for i, pnt in enumerate(self.points[::step]):
                pt = QPointF(*self._world(pnt[0], pnt[1], z))
                if i == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
            self._routes[z] = path
        return path

    # ---- mapa de fondo
    def _layer_file(self):
        return os.path.join(self.cache_dir, "layer.txt")

    def _load_layer(self):
        try:
            with open(self._layer_file(), "r", encoding="utf-8") as f:
                name = f.read().strip()
            if name in MAP_LAYERS:
                return name
        except Exception:
            pass
        return DEFAULT_LAYER

    def set_layer(self, name):
        if name not in MAP_LAYERS or name == self.layer:
            return
        self.layer = name
        self.tiles.clear()          # libera la memoria del mapa anterior (en disco queda)
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._layer_file(), "w", encoding="utf-8") as f:
                f.write(name)
        except Exception:
            pass
        self._invalidate()

    def _show_layer_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: #1f2230; color: #fff; border: 1px solid rgba(255,255,255,0.18);"
                           " border-radius: 8px; padding: 4px; }"
                           " QMenu::item { padding: 6px 18px 6px 26px; border-radius: 5px; }"
                           " QMenu::item:selected { background: rgba(255,255,255,0.12); }")
        group = QActionGroup(menu)
        for name, info in MAP_LAYERS.items():
            act = QAction(tr(self.lang, info["label"]), menu)
            act.setCheckable(True)
            act.setChecked(name == self.layer)
            act.triggered.connect(lambda _c=False, n=name: self.set_layer(n))
            group.addAction(act)
            menu.addAction(act)
        b = self._layer_btn
        menu.exec(b.mapToGlobal(b.rect().bottomRight()) - QPoint(menu.sizeHint().width(), 0))

    def _tile_path(self, layer, z, x, y):
        # el mapa de OpenStreetMap sigue en la carpeta de siempre (teselas ya guardadas)
        base = self.cache_dir if layer == "osm" else os.path.join(self.cache_dir, layer)
        return os.path.join(base, str(z), str(x), f"{y}.{MAP_LAYERS[layer]['ext']}")

    def _cached_tile(self, z, x, y):
        """Tesela ya en memoria o en disco, sin descargarla."""
        key = f"{self.layer}/{z}/{x}/{y}"
        pm = self.tiles.get(key)
        if pm is None:
            path = self._tile_path(self.layer, z, x, y)
            if os.path.exists(path):
                pm = QPixmap(path)
                if pm.isNull():
                    return None
                self.tiles[key] = pm
        return pm

    def _tile(self, z, x, y):
        pm = self._cached_tile(z, x, y)
        if pm is not None:
            return pm
        key = f"{self.layer}/{z}/{x}/{y}"
        if key not in self.pending and time.monotonic() - self._failed.get(key, -60) > 30:
            self.pending.add(key)
            info = MAP_LAYERS[self.layer]
            subs = info["subs"]
            url = info["url"].format(s=subs[(x + y) % len(subs)] if subs else "", z=z, x=x, y=y)
            self.pool.submit(self._download, key, url, self._tile_path(self.layer, z, x, y))
        return None

    def _download(self, key, url, path):
        try:
            # Si el servidor corta la conexion se reintenta un par de veces
            # con una pausa.
            for attempt in range(3):
                try:
                    with _http_get(url, timeout=15) as r:
                        data = r.read()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(0.6 * (attempt + 1))
            if QImage.fromData(data).isNull():      # p. ej. un error en XML en vez de imagen
                raise ValueError("la respuesta no es una imagen")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            self.tile_loaded.emit(key)
        except Exception as e:
            print("[Mapa]", key, e)
            # se vuelve a intentar mas tarde (al mover o redibujar el mapa)
            self._failed[key] = time.monotonic()
            self.pending.discard(key)

    # ---- dibujo
    def paintEvent(self, event):
        key = (self.zoom, round(self.scale, 4), round(self.cx, 1), round(self.cy, 1),
               self.width(), self.height(), self.devicePixelRatioF())
        if self._bg is None or self._bg_key != key:
            dpr = self.devicePixelRatioF()
            bg = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
            bg.setDevicePixelRatio(dpr)
            p = QPainter(bg)
            self._paint_background(p)
            p.end()
            self._bg, self._bg_key = bg, key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._bg)
        if self.marker is not None and 0 <= self.marker < len(self.points):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#0d0d12"), 3))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(self._point_screen(self.marker), 9, 9)
            self._paint_telemetry(painter)
        painter.end()

    def _paint_background(self, painter):
        import math
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#20232e"))
        z = self.zoom
        vw, vh = self._view_wh()
        left = self.cx - vw / 2
        top = self.cy - vh / 2
        painter.save()
        painter.scale(self.scale, self.scale)
        # sin antialias y con 1 px de solapamiento: con la escala fraccionaria
        # quedaban lineas finas entre tesela y tesela
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        ov = 1.0 / self.scale
        info = MAP_LAYERS[self.layer]
        lz = z + info.get("detail", 0)      # nivel de las teselas que se dibujan
        ts = 256 / (1 << (lz - z))          # su tamano en pixeles del mundo al zoom z
        n = 2 ** lz
        # Mas alla del ultimo zoom del servidor se amplia la tesela de ese nivel.
        tz = min(lz, info["max"])
        for tx in range(int(math.floor(left / ts)), int(math.floor((left + vw) / ts)) + 1):
            for ty in range(int(math.floor(top / ts)), int(math.floor((top + vh) / ts)) + 1):
                if ty < 0 or ty >= n:
                    continue
                wx = tx % n
                dest = QRectF(tx * ts - left, ty * ts - top, ts + ov, ts + ov)
                d0 = d = lz - tz
                pm = self._tile(tz, wx >> d, ty >> d)
                # Mientras baja, se muestra ampliada una tesela de menos zoom
                # que ya este guardada (en vez de un hueco vacio).
                while pm is None and d < d0 + 4 and lz - d > 0:
                    d += 1
                    pm = self._cached_tile(lz - d, wx >> d, ty >> d)
                if pm is not None:
                    k = 1 << d
                    sub = pm.width() / k
                    painter.drawPixmap(dest, pm, QRectF((wx % k) * sub, (ty % k) * sub, sub, sub))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.points:
            painter.translate(-left, -top)
            route = self._route(z)
            s = self.scale
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 140), 7 / s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(route)
            painter.setPen(QPen(self.accent, 4 / s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(route)
        painter.restore()
        if self.points:
            for idx, color in ((0, "#2ecc71"), (len(self.points) - 1, "#ff5c5c")):
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.setBrush(QColor(color))
                painter.drawEllipse(self._point_screen(idx), 7, 7)
        # Atribucion obligatoria del mapa de fondo
        text = MAP_LAYERS[self.layer]["attrib"]
        f = painter.font()
        f.setPixelSize(11)
        painter.setFont(f)
        tw = painter.fontMetrics().horizontalAdvance(text) + 12
        r = QRectF(self.width() - tw - 4, self.height() - 20, tw, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 200))
        painter.drawRoundedRect(r, 4, 4)
        painter.setPen(QColor("#333333"))
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    TELE_RECT = QRectF(10, 10, 214, 104)

    def _telemetry(self, idx):
        """Velocidad (promedio de unos puntos alrededor), altura, distancia
        recorrida / total y tiempo transcurrido / total."""
        lo, hi = max(0, idx - 4), min(len(self.points), idx + 5)
        speed = sum(p[3] for p in self.points[lo:hi]) / max(1, hi - lo) * 3.6
        alt = self.points[idx][2]
        done, total = self._cum[idx], self._cum[-1]
        t = self.elapsed * idx / max(1, len(self.points) - 1) if self.elapsed else None
        return speed, alt, done, total, t

    @staticmethod
    def _fmt_km(m):
        return f"{m / 1000:.1f} km" if m >= 1000 else f"{m:.0f} m"

    @staticmethod
    def _fmt_time(sec):
        sec = int(sec)
        h, rem = divmod(sec, 3600)
        return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"

    def _paint_telemetry(self, painter):
        speed, alt, done, total, t = self._telemetry(self.marker)
        r = self.TELE_RECT
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 30, 215))
        painter.drawRoundedRect(r, 12, 12)
        f = painter.font()
        f.setBold(True)
        f.setPixelSize(30)
        painter.setFont(f)
        painter.setPen(self.accent)
        painter.drawText(QRectF(r.left() + 14, r.top() + 6, r.width() - 20, 38),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{speed:.0f}")
        num_w = painter.fontMetrics().horizontalAdvance(f"{speed:.0f}")
        f.setPixelSize(13)
        painter.setFont(f)
        painter.setPen(QColor(255, 255, 255, 170))
        painter.drawText(QRectF(r.left() + 20 + num_w, r.top() + 18, 60, 22),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "km/h")
        f.setBold(False)
        f.setPixelSize(12)
        painter.setFont(f)
        painter.setPen(QColor(255, 255, 255, 220))
        lines = [f"▲  {alt:.0f} m", f"↔  {self._fmt_km(done)} / {self._fmt_km(total)}"]
        if t is not None:
            lines.append(f"⏱  {self._fmt_time(t)} / {self._fmt_time(self.elapsed)}")
        for i, line in enumerate(lines):
            painter.drawText(QRectF(r.left() + 14, r.top() + 46 + i * 18, r.width() - 20, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)

    # ---- interaccion
    def mousePressEvent(self, event):
        self._drag = (event.position(), self.cx, self.cy)
        self._user_moved = True
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag:
            p0, cx, cy = self._drag
            d = event.position() - p0
            self.cx, self.cy = cx - d.x() / self.scale, cy - d.y() / self.scale
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):
        self.zoom_at(1 if event.angleDelta().y() > 0 else -1, event.position())

    def mouseDoubleClickEvent(self, event):
        self.zoom_at(1, event.position())      # doble clic: acercar en ese punto

    def refit(self):
        """Vuelve a encuadrar todo el recorrido."""
        self._user_moved = False
        self.fit()
        self._invalidate()

    def zoom_at(self, step, pos=None):
        """Acerca/aleja x2 manteniendo fijo el punto bajo el mouse."""
        self._user_moved = True
        nz = max(2, min(19, self.zoom + step))
        if nz == self.zoom:
            return
        if pos is None:
            pos = QPointF(self.width() / 2, self.height() / 2)
        vw, vh = self._view_wh()
        wx = self.cx - vw / 2 + pos.x() / self.scale
        wy = self.cy - vh / 2 + pos.y() / self.scale
        f = 2 ** (nz - self.zoom)
        self.zoom = nz
        self.cx = wx * f - pos.x() / self.scale + vw / 2
        self.cy = wy * f - pos.y() / self.scale + vh / 2
        self.update()

    def _marker_rect(self, idx):
        if idx is None or not (0 <= idx < len(self.points)) or not self._fitted:
            return None
        c = self._point_screen(idx)
        return QRectF(c.x() - 13, c.y() - 13, 26, 26).toAlignedRect()

    def set_progress(self, fraction):
        """Mueve el punto blanco a la parte del recorrido que se esta viendo."""
        if not self.points:
            return
        idx = int(max(0.0, min(1.0, fraction)) * (len(self.points) - 1))
        if idx != self.marker:
            old, self.marker = self._marker_rect(self.marker), idx
            # redibuja solo el punto (donde estaba y donde queda) y la telemetria
            for r in (old, self._marker_rect(idx), self.TELE_RECT.toAlignedRect().adjusted(-2, -2, 2, 2)):
                if r is not None:
                    self.update(r)

    def shutdown(self):
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


class GpsMapDialog(QDialog):
    """Recorrido GPS de un video: mapa + distancia, velocidades y altura."""

    def __init__(self, parent, lang, path, accent):
        super().__init__(parent)
        self.lang, self.path, self.accent = lang, path, accent
        self.map = None
        self.setWindowTitle(tr(lang, "gps_title", name=os.path.basename(path)))
        self.resize(1000, 720)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1e29; }}
            QLabel {{ color: #fff; background: transparent; }}
            * {{ font-family: {FONT_STACK}; }}
            QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
        """)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(22, 18, 22, 16)
        self.lay.setSpacing(10)
        title = QLabel(tr(lang, "gps_title", name=os.path.basename(path)))
        title.setStyleSheet("font-size: 18px; font-weight: 800;")
        self.lbl_stats = QLabel(tr(lang, "gps_reading"))
        self.lbl_stats.setWordWrap(True)
        self.lbl_stats.setStyleSheet(f"font-size: 13px; color: {accent};")
        self.lay.addWidget(title)
        self.lay.addWidget(self.lbl_stats)
        self.map_slot = QVBoxLayout()
        self.lay.addLayout(self.map_slot, 1)
        foot = QHBoxLayout()
        self.lbl_hint = QLabel(tr(lang, "gps_hint"))
        self.lbl_hint.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.45);")
        self.btn_osm = QPushButton(tr(lang, "gps_open_osm"))
        self.btn_osm.hide()
        close = QPushButton(tr(lang, "close_btn2"))
        close.clicked.connect(self.accept)
        foot.addWidget(self.lbl_hint)
        foot.addStretch()
        foot.addWidget(self.btn_osm)
        foot.addWidget(close)
        self.lay.addLayout(foot)
        th = ScanThread(0, read_gopro_track, path)
        th.done.connect(self._on_points)
        th.start()

    def _on_points(self, _t, st):
        pts = st["points"] if st else None
        if not pts:
            self.lbl_stats.setText(tr(self.lang, "gps_nofix" if st and st.get("nofix") else "gps_none"))
            self.lbl_stats.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.7);")
            self.lbl_hint.hide()
            return
        # tiempo real grabado: en un time-lapse es mucho mas que lo que dura el video
        duration = st.get("elapsed") or read_mp4_info(self.path).get("duration") or 0
        if st:
            dist = f"{st['dist'] / 1000:.1f} km" if st["dist"] >= 1000 else f"{st['dist']:.0f} m"
            mins = int(duration // 60)
            dur = f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"
            self.lbl_stats.setText(tr(self.lang, "gps_stats", dist=dist, vmax=f"{st['vmax']:.0f}", vavg=f"{st['vavg']:.0f}",
                                      amin=f"{st['amin']:.0f}", amax=f"{st['amax']:.0f}", dur=dur))
        self.map = TileMapWidget(pts, self.accent, self, lang=self.lang)
        self.map_slot.addWidget(self.map)
        lat, lon = pts[len(pts) // 2][0], pts[len(pts) // 2][1]
        self.btn_osm.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=14/{lat:.5f}/{lon:.5f}")))
        self.btn_osm.show()

    def done(self, r):
        if self.map is not None:
            self.map.shutdown()
        super().done(r)
