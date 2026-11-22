"""Mapa con el recorrido GPS (callejero, satelite, topografico...)."""
import bisect
import math
import os
import threading
import time
from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget
from ..env import CACHE_DIR, FONT_STACK, load_config
from ..i18n import tr
from ..media import cumulative_distance, read_gopro_track, read_mp4_info
from ..workers import ScanThread
from ..version import APP_VERSION

# Mapas de fondo gratuitos, sin clave y de todo el mundo. {s} = subdominio para repartir las
# descargas; "max" = ultimo zoom que tiene el servidor (mas alla se amplia
# la tesela de ese nivel); "detail" = niveles de zoom de mas que se piden
# (1 = teselas de la mitad de tamano: el doble de resolucion). Solo en las
# fotos aereas: en un callejero achicaria los nombres de las calles.
# (CARTO pide clave desde 2026: sus teselas solo dicen "API KEY REQUIRED".)
MAP_LAYERS = {
    "satellite": {
        "label": "map_layer_satellite",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}?blankTile=false",
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
# Donde no hay foto con tanto zoom, Esri devolvia (sin blankTile=false) esta
# imagen gris "Map data not yet available" como si fuera una tesela: tapaba
# la foto ampliada del nivel anterior. Las que ya quedaron guardadas se
# descartan por su huella.
_BLANK_TILE_SHA1 = {"1660d86a87f57ef0ff580822e0e62f0feb48deee"}


def _is_blank_tile(data):
    import hashlib
    return len(data) < 8192 and hashlib.sha1(data).hexdigest() in _BLANK_TILE_SHA1


class _HttpStatus(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


_SSL_CTX = None
_SSL_LOCK = threading.Lock()
_TLS = threading.local()


def _ssl_context():
    # Uno solo para todas las descargas: crearlo carga los certificados del
    # sistema (~0,1 s con el GIL tomado) y urllib lo hacia en CADA pedido,
    # trabando la ventana mientras bajaban las teselas.
    global _SSL_CTX
    with _SSL_LOCK:
        if _SSL_CTX is None:
            import ssl
            _SSL_CTX = ssl.create_default_context()
        return _SSL_CTX


def _tile_get(url, timeout=15):
    """GET de una tesela reutilizando la conexion (keep-alive) del hilo con
    ese servidor: sin un apreton de manos TLS por tesela."""
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(url)
    conns = getattr(_TLS, "conns", None)
    if conns is None:
        conns = _TLS.conns = {}
    target = u.path + ("?" + u.query if u.query else "")
    for attempt in range(2):
        conn = conns.get(u.netloc)
        if conn is None:
            conn = http.client.HTTPSConnection(u.netloc, timeout=timeout, context=_ssl_context())
            conns[u.netloc] = conn
        try:
            conn.request("GET", target, headers={"User-Agent": f"GoProSync/{APP_VERSION}"})
            r = conn.getresponse()
            data = r.read()
        except (http.client.HTTPException, OSError):
            conn.close()                    # el servidor cerro la conexion: otra nueva
            conns.pop(u.netloc, None)
            if attempt:
                raise
            continue
        if r.status != 200:
            raise _HttpStatus(r.status)
        return data


class TileMapWidget(QWidget):
    """Mapa con el recorrido: arrastrar para mover, rueda, doble clic o
    botones para acercar; un boton elige el mapa de fondo (callejero,
    satelite...). Las teselas se guardan en disco para no volver a pedirlas.
    En el reproductor muestra ademas la telemetria del punto que se esta
    viendo (velocidad, altura, distancia y tiempo).

    Para que la ventana no se trabe (como en las apps de mapas):
    - Los hilos de fondo solo leen del disco o descargan bytes. En PySide
      cualquier llamada a Qt desde un hilo (decodificar o dibujar) retiene el
      GIL y deja a la ventana esperando todo ese tiempo; leer/descargar no.
    - Las teselas se decodifican en la ventana de a tandas cortas (~6 ms) y
      quedan en memoria (las menos usadas se descartan).
    - El fondo se guarda en dos capas ya dibujadas: teselas y recorrido. Al
      llegar una tesela solo se rehace la de teselas; el recorrido se
      recorta a lo que se ve y solo se rehace si cambia la vista.
    - Al arrastrar o hacer zoom se muestran las capas que ya estan, movidas
      o ampliadas (instantaneo), y se rehacen como mucho cada ~120 ms."""
    tile_data = Signal(str, bytes)      # tesela, contenido (vacio si no se consiguio)
    MAX_TILES = 320                     # teselas en memoria (~80 MB); el resto queda en disco
    RENDER_EVERY_MS = 120               # mientras se mueve el mapa, rehacer capas cada tanto
    DECODE_BUDGET_S = 0.006             # tiempo por tanda de decodificacion en la ventana
    FIT_MARGIN = 28                 # px libres alrededor del recorrido al encuadrar

    def __init__(self, points, accent, parent=None, elapsed=None, lang="es", cum=None):
        super().__init__(parent)
        from concurrent.futures import ThreadPoolExecutor
        self.points = points
        self.accent = QColor(accent)
        self.lang = lang
        self.elapsed = elapsed or 0     # segundos reales grabados (para el reloj de la telemetria)
        from collections import OrderedDict, deque
        self.tiles = OrderedDict()      # tesela -> QImage, la menos usada primero
        self.pending = set()
        self._decode_queue = deque()    # (tesela, bytes) por decodificar
        self._tiles_layer = None        # (QImage, vista) teselas ya pegadas
        self._route_layer = None        # (QImage, vista) recorrido ya dibujado
        self._tiles_dirty = True
        self._last_render = 0.0
        self._route_pts = {}            # zoom -> recorrido simplificado en pixeles del mundo
        self._failed = {}               # tesela -> momento del ultimo fallo
        self.pool = ThreadPoolExecutor(max_workers=4)
        # Windows: %LOCALAPPDATA%\GoProSyncPro\tiles como siempre; macOS/Linux: cache del usuario.
        self.cache_dir = os.path.join(CACHE_DIR, "tiles")
        # Privacidad: con "Descargar mapas de internet" apagado en Ajustes no se
        # pide nada a los servidores de teselas (verian donde se grabo el video).
        try:
            self.online = bool(load_config().get("map_online", True))
        except Exception:
            self.online = True
        self.layer = self._load_layer()
        # Vista: zoom entero de las teselas + escala 1..2 encima, para que el
        # recorrido llene el panel (con zoom entero quedaba a veces a la mitad).
        self.zoom = 3
        self.scale = 1.0
        self.cx = self.cy = 0.0         # centro, en pixeles del mundo al zoom entero
        self._drag = None
        self.setMinimumSize(640, 420)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.tile_data.connect(self._on_tile_data)
        self._decode_timer = QTimer(self)
        self._decode_timer.setSingleShot(True)
        self._decode_timer.setInterval(0)
        self._decode_timer.timeout.connect(self._decode_some)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_layers)
        self._fitted = False
        self._user_moved = False
        self.marker = None              # indice del punto actual (reproductor)
        self._base = None               # recorrido proyectado a zoom 0 (se calcula una vez)
        # Distancia acumulada: viene calculada con el recorrido (en segundo
        # plano); calcularla aca, con miles de puntos, trababa la ventana.
        self._cum = cum if cum is not None and len(cum) == len(points) else cumulative_distance(points)
        if points:
            lats = [p[0] for p in points]
            lons = [p[1] for p in points]
            self._bounds = (min(lats), max(lats), min(lons), max(lons))
        else:
            self._bounds = None
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

    def fit(self):
        if not self._bounds:
            return
        lat_min, lat_max, lon_min, lon_max = self._bounds
        w = max(100, self.width() - 2 * self.FIT_MARGIN)
        h = max(100, self.height() - 2 * self.FIT_MARGIN)
        z = 18
        while z > 2:
            x1, y1 = self._world(lat_max, lon_min, z)
            x2, y2 = self._world(lat_min, lon_max, z)
            if abs(x2 - x1) <= w and abs(y2 - y1) <= h:
                break
            z -= 1
        x1, y1 = self._world(lat_max, lon_min, z)
        x2, y2 = self._world(lat_min, lon_max, z)
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
        for i, b in enumerate(self._zoom_btns):
            b.move(self.width() - 44, 10 + i * 40)

    # ---- teselas
    def _invalidate(self):
        """Rehace las capas aunque la vista no haya cambiado (otro mapa de
        fondo, volver a encuadrar...)."""
        self._tiles_layer = self._route_layer = None
        self._tiles_dirty = True
        self.update()

    def _route_world(self, z):
        """Recorrido en pixeles del mundo al zoom z, sin los puntos que caen a
        menos de un pixel del anterior. Se proyecta una sola vez (a zoom 0) y
        se guarda por zoom."""
        pts = self._route_pts.get(z)
        if pts is None:
            if self._base is None:
                src = self.points
                step = max(1, len(src) // 8000)
                sample = src[::step]
                if len(src) > 1 and sample[-1] is not src[-1]:
                    sample.append(src[-1])
                self._base = [self._world(p[0], p[1], 0) for p in sample]
            f = float(1 << z)
            pts, lx, ly = [], None, None
            for bx, by in self._base:
                x, y = bx * f, by * f
                if lx is None or abs(x - lx) + abs(y - ly) >= 1.0:
                    pts.append((x, y))
                    lx, ly = x, y
            if self._base:
                last = (self._base[-1][0] * f, self._base[-1][1] * f)
                if pts[-1] != last:
                    pts.append(last)
            self._route_pts[z] = pts
        return pts

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
        self._decode_queue.clear()
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
        """Tesela que ya esta en memoria (no lee el disco ni descarga)."""
        key = f"{self.layer}/{z}/{x}/{y}"
        img = self.tiles.get(key)
        if img is not None:
            self.tiles.move_to_end(key)
        return img

    def _tile(self, z, x, y):
        """Tesela en memoria; si no esta, la pide en segundo plano (del disco
        o de internet) y devuelve None: la ventana nunca espera por ella."""
        key = f"{self.layer}/{z}/{x}/{y}"
        img = self.tiles.get(key)
        if img is not None:
            self.tiles.move_to_end(key)
            return img
        if key not in self.pending and time.monotonic() - self._failed.get(key, -60) > 30:
            self.pending.add(key)
            info = MAP_LAYERS[self.layer]
            subs = info["subs"]
            url = info["url"].format(s=subs[(x + y) % len(subs)] if subs else "", z=z, x=x, y=y)
            try:
                self.pool.submit(self._fetch, key, url, self._tile_path(self.layer, z, x, y), self.online)
            except RuntimeError:
                pass                            # el mapa ya se cerro
        return None

    def _fetch(self, key, url, path, online):
        """En un hilo aparte, SIN llamar a Qt (retendria el GIL y trabaria la
        ventana): lee la tesela del disco o la descarga y la guarda."""
        data = b""
        try:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
                if _is_blank_tile(data):
                    os.remove(path)          # gris "sin datos" guardado antes
                    data = b""
            if not data and online:
                # Si el servidor corta la conexion se reintenta un par de
                # veces con una pausa; un 404 (no hay foto a ese zoom) no.
                for attempt in range(3):
                    try:
                        data = _tile_get(url)
                        break
                    except Exception as e:
                        if attempt == 2 or getattr(e, "code", None) == 404:
                            raise
                        time.sleep(0.6 * (attempt + 1))
                if _is_blank_tile(data):
                    raise ValueError("sin foto a este zoom")
                if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("la respuesta no es una imagen")   # p. ej. un error en XML
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
        except Exception as e:
            data = b""
            if getattr(e, "code", None) != 404:
                print("[Mapa]", key, e)
        try:
            self.tile_data.emit(key, data)
        except RuntimeError:
            pass                             # el mapa ya se cerro

    def _on_tile_data(self, key, data):
        if not data:
            self.pending.discard(key)
            self._failed[key] = time.monotonic()   # se reintenta mas tarde
            return
        self._decode_queue.append((key, data))
        if not self._decode_timer.isActive():
            self._decode_timer.start()

    def _decode_some(self):
        """Decodifica teselas en la ventana, pocas por vez: entre tanda y
        tanda la ventana atiende al usuario."""
        t0 = time.perf_counter()
        got = False
        while self._decode_queue and time.perf_counter() - t0 < self.DECODE_BUDGET_S:
            key, data = self._decode_queue.popleft()
            self.pending.discard(key)
            if not key.startswith(self.layer + "/"):
                continue                     # de otro mapa de fondo
            img = QImage.fromData(data)
            if img.isNull():
                self._failed[key] = time.monotonic()
                continue
            self.tiles[key] = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            got = True
        while len(self.tiles) > self.MAX_TILES:
            self.tiles.popitem(last=False)
        if self._decode_queue:
            self._decode_timer.start()
        if got:
            self._tiles_dirty = True
            self._schedule_render()

    # ---- dibujo
    def _view(self):
        return (self.zoom, round(self.scale, 4), round(self.cx, 1), round(self.cy, 1),
                self.width(), self.height(), self.devicePixelRatioF(), self.layer)

    def paintEvent(self, event):
        view = self._view()
        if (self._tiles_dirty or self._tiles_layer is None or self._tiles_layer[1] != view
                or self._route_layer is None or self._route_layer[1] != view):
            self._schedule_render()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#20232e"))
        # Capas ya dibujadas, llevadas a la vista actual: al arrastrar o hacer
        # zoom se ven al instante (las nuevas llegan enseguida).
        for layer in (self._tiles_layer, self._route_layer):
            if layer is None:
                continue
            img, lv = layer
            k, ox, oy = self._layer_transform(lv, view)
            painter.save()
            if k != 1.0:
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.translate(ox, oy)
            painter.scale(k, k)
            painter.drawImage(0, 0, img)
            painter.restore()
        self._paint_attribution(painter)
        if self.marker is not None and 0 <= self.marker < len(self.points):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#0d0d12"), 3))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(self._point_screen(self.marker), 9, 9)
            self._paint_telemetry(painter)
        painter.end()

    @staticmethod
    def _layer_transform(lv, view):
        """Escala y corrimiento que llevan una capa dibujada con la vista lv
        a la vista actual."""
        lz, ls, lcx, lcy, lw, lh = lv[:6]
        z, s, cx, cy, w, h = view[:6]
        f = 2.0 ** (z - lz)
        k = s * f / ls
        ox = s * f * (lcx - lw / ls / 2) - s * (cx - w / s / 2)
        oy = s * f * (lcy - lh / ls / 2) - s * (cy - h / s / 2)
        return k, ox, oy

    def _schedule_render(self):
        if self._render_timer.isActive():
            return
        wait = self.RENDER_EVERY_MS / 1000.0 - (time.perf_counter() - self._last_render)
        self._render_timer.start(max(0, int(wait * 1000)))

    def _render_layers(self):
        view = self._view()
        if view[4] <= 0 or view[5] <= 0:
            return
        self._last_render = time.perf_counter()
        if self._tiles_dirty or self._tiles_layer is None or self._tiles_layer[1] != view:
            self._tiles_dirty = False
            self._tiles_layer = (self._render_tiles(view), view)
        if self._route_layer is None or self._route_layer[1] != view:
            self._route_layer = (self._render_route(view), view)
        self.update()

    def _new_layer(self, view, fill=None):
        z, s, cx, cy, w, h, dpr, layer = view
        img = QImage(int(w * dpr), int(h * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(fill if fill is not None else QColor(0, 0, 0, 0))
        return img

    def _render_tiles(self, view):
        z, s, cx, cy, w, h, dpr, layer = view
        img = self._new_layer(view, QColor("#20232e"))
        vw, vh = w / s, h / s
        left, top = cx - vw / 2, cy - vh / 2
        info = MAP_LAYERS[layer]
        lz = z + info.get("detail", 0)      # nivel de las teselas que se dibujan
        ts = 256 / (1 << (lz - z))          # su tamano en pixeles del mundo al zoom z
        n = 2 ** lz
        # Mas alla del ultimo zoom del servidor se amplia la tesela de ese nivel.
        tz = min(lz, info["max"])
        ov = 1.0 / s
        p = QPainter(img)
        p.scale(s, s)
        # 1 px de solapamiento: con la escala fraccionaria quedaban lineas
        # finas entre tesela y tesela
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for tx in range(int(math.floor(left / ts)), int(math.floor((left + vw) / ts)) + 1):
            for ty in range(int(math.floor(top / ts)), int(math.floor((top + vh) / ts)) + 1):
                if ty < 0 or ty >= n:
                    continue
                wx = tx % n
                d0 = d = lz - tz
                tile = self._tile(tz, wx >> d, ty >> d)
                # Mientras baja (o si no hay foto con tanto zoom), se muestra
                # ampliada una tesela de menos zoom que ya este en memoria.
                while tile is None and d < d0 + 6 and lz - d > 0:
                    d += 1
                    tile = self._cached_tile(lz - d, wx >> d, ty >> d)
                if tile is None and tz > 2:
                    self._tile(tz - 1, wx >> (d0 + 1), ty >> (d0 + 1))   # vista previa del nivel anterior
                if tile is not None:
                    k = 1 << d
                    sub = tile.width() / k
                    p.drawImage(QRectF(tx * ts - left, ty * ts - top, ts + ov, ts + ov), tile,
                                QRectF((wx % k) * sub, (ty % k) * sub, sub, sub))
        p.end()
        return img

    def _render_route(self, view):
        """Recorrido (solo la parte que se ve) + puntos de inicio y fin."""
        z, s, cx, cy, w, h, dpr, layer = view
        img = self._new_layer(view)
        if not self.points:
            return img
        vw, vh = w / s, h / s
        left, top = cx - vw / 2, cy - vh / 2
        m = 20 / s                           # margen: que no se corte el grosor de la linea
        x0, y0, x1, y1 = left - m, top - m, left + vw + m, top + vh + m
        path = QPainterPath()
        prev, prev_in, open_ = None, False, False
        for x, y in self._route_world(z):
            inside = x0 <= x <= x1 and y0 <= y <= y1
            # un tramo se dibuja si alguna punta se ve o si cruza la vista
            if prev is not None and (inside or prev_in or
                                     not (max(x, prev[0]) < x0 or min(x, prev[0]) > x1 or
                                          max(y, prev[1]) < y0 or min(y, prev[1]) > y1)):
                if not open_:
                    path.moveTo(prev[0] - left, prev[1] - top)
                    open_ = True
                path.lineTo(x - left, y - top)
            else:
                open_ = False
            prev, prev_in = (x, y), inside
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(s, s)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 140), 7 / s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(path)
        p.setPen(QPen(self.accent, 4 / s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(path)
        p.resetTransform()
        for idx, color in ((0, "#2ecc71"), (len(self.points) - 1, "#ff5c5c")):
            wx, wy = self._world(self.points[idx][0], self.points[idx][1], z)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF((wx - left) * s, (wy - top) * s), 7, 7)
        p.end()
        return img

    def _paint_attribution(self, painter):
        # Atribucion obligatoria del mapa de fondo
        painter.save()
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
        if not self.online:
            note = tr(self.lang, "map_offline_notice")
            nw = painter.fontMetrics().horizontalAdvance(note) + 12
            nr = QRectF(4, self.height() - 20, min(nw, self.width() - tw - 12), 18)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(nr, 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(nr, Qt.AlignmentFlag.AlignCenter, note)
        painter.restore()

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
        fr = max(0.0, min(1.0, fraction))
        fracs = getattr(self, "fracs", None)
        if fracs and len(fracs) == len(self.points):
            # en que parte del video esta cada punto: si el GPS tardo en
            # agarrar senal, el recorrido no empieza en el segundo 0
            idx = min(len(self.points) - 1, bisect.bisect_left(fracs, fr))
        else:
            idx = int(fr * (len(self.points) - 1))
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
        # Guardar el recorrido como GPX / KML (lo hace la ventana principal)
        self.btn_gpx = QPushButton(tr(lang, "track_gpx_btn"))
        self.btn_kml = QPushButton(tr(lang, "track_kml_btn"))
        for b, fmt in ((self.btn_gpx, "gpx"), (self.btn_kml, "kml")):
            b.setToolTip(tr(lang, "track_gpx" if fmt == "gpx" else "track_kml"))
            b.clicked.connect(lambda _c=False, f=fmt: self._export(f))
            b.hide()
        self.btn_overlay = QPushButton(tr(lang, "ov_btn"))
        self.btn_overlay.setToolTip(tr(lang, "ov_menu"))
        self.btn_overlay.clicked.connect(self._overlay)
        self.btn_overlay.hide()
        close = QPushButton(tr(lang, "close_btn2"))
        close.clicked.connect(self.accept)
        foot.addWidget(self.lbl_hint)
        foot.addStretch()
        foot.addWidget(self.btn_gpx)
        foot.addWidget(self.btn_kml)
        foot.addWidget(self.btn_overlay)
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
        self.map = TileMapWidget(pts, self.accent, self, lang=self.lang, cum=st.get("cum"))
        self.map_slot.addWidget(self.map)
        lat, lon = pts[len(pts) // 2][0], pts[len(pts) // 2][1]
        self.btn_osm.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=14/{lat:.5f}/{lon:.5f}")))
        self.btn_osm.show()
        main = self.parent().window() if self.parent() is not None else None
        if main is not None and hasattr(main, "export_track"):
            self.btn_gpx.show()
            self.btn_kml.show()
        if main is not None and hasattr(main, "make_overlay"):
            self.btn_overlay.show()

    def _overlay(self):
        main = self.parent().window() if self.parent() is not None else None
        if main is not None and hasattr(main, "make_overlay"):
            main.make_overlay(self.path, parent=self)

    def _export(self, fmt):
        main = self.parent().window() if self.parent() is not None else None
        if main is not None and hasattr(main, "export_track"):
            main.export_track(self.path, fmt, parent=self)

    def done(self, r):
        if self.map is not None:
            self.map.shutdown()
        super().done(r)
