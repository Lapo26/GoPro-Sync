"""Miniaturas (cache en disco) y la grilla virtualizada de la galeria."""
import hashlib
import os
import shutil
import struct
import subprocess
import threading
from datetime import datetime
from datetime import timedelta
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QToolTip
from ..env import CONFIG_DIR, _hidden_startupinfo
from ..i18n import tr
from ..media import is_broken_mp4
from ..fileops import MEDIA_EXTENSIONS_BY_MODE
from ..videotools import ffmpeg_exe
from ..ui.graphics import THUMB_H, THUMB_W, draw_play_badge, make_cover_thumbnail, screen_dpr
from ..ui.theme import current_accent

# Miniaturas de video: el primer cuadro con el ffmpeg incluido (el mismo que
# usan unir/reparar). Antes se usaba OpenCV solo para esto: +140 MB en el
# paquete y ~1 s para importarlo.
# Prioridad normal a proposito: con BELOW_NORMAL, si la PC esta ocupada,
# algunas miniaturas tardaban ~30 s en aparecer.
_FFMPEG_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


DEFAULT_THUMB_CACHE_DIR = os.path.join(CONFIG_DIR, "thumb_cache")


def set_thumb_cache_dir(path):
    ThumbnailLoaderThread.CACHE_DIR = path or DEFAULT_THUMB_CACHE_DIR


def move_thumb_cache(old_dir, new_dir):
    """Mueve las miniaturas existentes a la nueva ubicacion. Devuelve cuantas."""
    moved = 0
    os.makedirs(new_dir, exist_ok=True)
    if os.path.isdir(old_dir) and os.path.normcase(os.path.abspath(old_dir)) != os.path.normcase(os.path.abspath(new_dir)):
        for name in os.listdir(old_dir):
            src = os.path.join(old_dir, name)
            if os.path.isfile(src) and name.endswith(".jpg"):
                try:
                    shutil.move(src, os.path.join(new_dir, name))
                    moved += 1
                except Exception:
                    pass
        try:
            os.rmdir(old_dir)
        except OSError:
            pass
    return moved


# Un solo hilo lee del disco a la vez: en discos mecanicos (HDD), que las
# fotos y los videos se lean en paralelo hace que el cabezal salte sin parar.
_DISK_LOCK = threading.Lock()
# Los videos NO usan ese candado: ffmpeg lee solo el indice y el primer cuadro
# (~3 MB) y casi todo el tiempo es CPU y arrancar el proceso. Varios a la vez
# dejan la galeria de videos mas rapida que antes con OpenCV de a uno, tambien
# en disco mecanico, y no frenan a las fotos.
_VIDEO_SLOTS = threading.Semaphore(4)


def _gen_lock(kind):
    """Candado para generar una miniatura nueva segun el tipo de archivo."""
    return _VIDEO_SLOTS if kind == "video" else _DISK_LOCK


def _trim_black_bars(img):
    """La miniatura EXIF de GoPro es 4:3 con bandas negras arriba/abajo
    (la foto es mas panoramica): se recortan."""
    h, w = img.height(), img.width()

    def dark(y):
        return all(QColor(img.pixel(x, y)).lightness() < 18 for x in range(0, w, max(1, w // 16)))
    top = 0
    while top < h // 3 and dark(top):
        top += 1
    bottom = h - 1
    while bottom > h * 2 // 3 and dark(bottom):
        bottom -= 1
    if top == 0 and bottom == h - 1:
        return img
    return img.copy(0, top, w, bottom - top + 1)


def read_exif_thumbnail(path, max_bytes=64 * 1024):
    """Miniatura JPEG incrustada en el EXIF (las GoPro la guardan). Solo lee
    el principio del archivo (unos KB), no la foto entera de varios MB."""
    try:
        with open(path, "rb") as f:
            head = f.read(max_bytes)
        if head[:2] != b"\xff\xd8":
            return None
        pos = 2
        while pos + 4 <= len(head):
            if head[pos] != 0xFF:
                return None
            marker = head[pos + 1]
            seg_len = struct.unpack(">H", head[pos + 2:pos + 4])[0]
            if marker == 0xE1 and head[pos + 4:pos + 10] == b"Exif\x00\x00":
                tiff = head[pos + 10:pos + 2 + seg_len]
                bo = "<" if tiff[:2] == b"II" else ">"
                ifd0 = struct.unpack(bo + "I", tiff[4:8])[0]
                n0 = struct.unpack(bo + "H", tiff[ifd0:ifd0 + 2])[0]
                ifd1 = struct.unpack(bo + "I", tiff[ifd0 + 2 + 12 * n0:ifd0 + 6 + 12 * n0])[0]
                if not ifd1:
                    return None
                n1 = struct.unpack(bo + "H", tiff[ifd1:ifd1 + 2])[0]
                off = length = None
                for i in range(n1):
                    e = tiff[ifd1 + 2 + 12 * i: ifd1 + 14 + 12 * i]
                    tag = struct.unpack(bo + "H", e[:2])[0]
                    val = struct.unpack(bo + "I", e[8:12])[0]
                    if tag == 0x0201:
                        off = val
                    elif tag == 0x0202:
                        length = val
                if off and length:
                    img = QImage.fromData(tiff[off:off + length])
                    return None if img.isNull() else _trim_black_bars(img)
                return None
            if marker in (0xDA, 0xD9):
                return None
            pos += 2 + seg_len
    except Exception:
        return None
    return None


class ThumbnailLoaderThread(QThread):
    """Erzeugt die Vorschaubilder fuer eine Galerie (Videos ODER Bilder) in
    einem Hintergrund-Thread, statt die Oberflaeche waehrend dessen
    einzufrieren - bei vielen Dateien (v.a. Videos, wo jeweils der erste
    Frame per ffmpeg dekodiert werden muss) machte das bisher spuerbar
    lange nichts mehr reagierte ("15 Sekunden nach dem Einrichtungsassistenten").
    Nutzt durchgehend QImage (nicht QPixmap) - das ist die einzige Qt-
    Bildklasse, die auch ausserhalb des GUI-Threads sicher ist."""
    thumbnail_ready = Signal(str, QImage)
    finished_loading = Signal()

    def __init__(self, files, kind, parent=None, meta=None):
        super().__init__(parent)
        self.files = files
        self.meta = meta or {}     # ruta -> (tamano, fecha) ya conocidos por el listado
        self.kind = kind  # "video" oder "picture"
        self.dpr = screen_dpr()  # se lee en el hilo de la UI
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    # Cache en disco de las miniaturas: antes se regeneraban TODAS en cada
    # apertura de la galeria (decodificando cada JPG de 12 MP completo y el
    # primer fotograma de cada video). Ahora se guarda una version reducida
    # (clave = ruta + tamano + fecha) y las siguientes cargas son casi
    # instantaneas.
    CACHE_DIR = DEFAULT_THUMB_CACHE_DIR   # se cambia con set_thumb_cache_dir()
    CACHE_W, CACHE_H = THUMB_W * 2, THUMB_H * 2   # 2x: nitidas hasta 200 % de escala

    def _cache_path(self, path):
        known = self.meta.get(path) if hasattr(self, "meta") else None
        if known is not None:
            size, mtime = known            # del listado de la carpeta: sin consultar el disco
        else:
            try:
                st = os.stat(path)
            except OSError:
                return None
            size, mtime = st.st_size, st.st_mtime
        key = f"{os.path.normcase(path)}|{size}|{int(mtime)}"
        return os.path.join(self.CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg")

    def _load_cached(self, path):
        cache_file = self._cache_path(path)
        if cache_file and os.path.exists(cache_file):
            img = QImage(cache_file)
            if not img.isNull():
                return img
        return None

    def _damaged_image(self):
        img = QImage(self.CACHE_W, self.CACHE_H, QImage.Format.Format_RGB32)
        img.fill(QColor("#2a1e22"))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#ff8a8a"))
        f = QFont()
        f.setPixelSize(int(self.CACHE_H * 0.12))
        f.setBold(True)
        p.setFont(f)
        p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, tr(getattr(self, "lang", "es"), "video_damaged"))
        p.end()
        return img

    def _load_raw(self, path):
        cache_file = self._cache_path(path)
        if self.kind == "video" and is_broken_mp4(path):
            return self._damaged_image()   # sin cache: si se repara, se regenera
        if self.kind == "video":
            raw = self._read_video_frame(path)
        else:
            # QImageReader con setScaledSize decodifica el JPEG directamente
            # a menor resolucion: mucho mas rapido que cargarlo entero.
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and size.width() > self.CACHE_W * 2:
                factor = max(self.CACHE_W / size.width(), self.CACHE_H / size.height())
                reader.setScaledSize(QSize(max(1, int(size.width() * factor * 1.5)),
                                           max(1, int(size.height() * factor * 1.5))))
            raw = reader.read()
        if raw is not None and not raw.isNull():
            raw = raw.scaled(self.CACHE_W, self.CACHE_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
            if cache_file:
                try:
                    os.makedirs(self.CACHE_DIR, exist_ok=True)
                    raw.save(cache_file, "JPG", 85)
                except Exception:
                    pass
        return raw

    def _emit(self, path, raw):
        cover = make_cover_thumbnail(raw, THUMB_W, THUMB_H, dpr=self.dpr)
        if self.kind == "video":
            cover = draw_play_badge(cover)
        if not self._stop_requested:
            self.thumbnail_ready.emit(path, cover)

    def run(self):
        # 1) Lo que ya esta en cache, y para las fotos nuevas la miniatura EXIF
        #    (lee pocos KB): la galeria se llena en segundos.
        # 2) Despues, la version nitida de lo que faltaba (se guarda en cache);
        #    la miniatura se reemplaza en su lugar.
        pending = []
        # Lo que ya esta en cache se carga EN PARALELO (4 a la vez; leer y
        # decodificar JPEG libera el GIL): antes era de a uno.
        from concurrent.futures import ThreadPoolExecutor

        def load(path):
            try:
                img = self._load_cached(path)
                if img is None:
                    return path, None
                cover = make_cover_thumbnail(img, THUMB_W, THUMB_H, dpr=self.dpr)
                if self.kind == "video":
                    cover = draw_play_badge(cover)
                return path, cover
            except Exception:
                return path, None

        not_cached = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            chunk = 64
            for i in range(0, len(self.files), chunk):
                if self._stop_requested:
                    return
                for path, cover in pool.map(load, self.files[i:i + chunk]):
                    if cover is None:
                        not_cached.append(path)
                    elif not self._stop_requested:
                        self.thumbnail_ready.emit(path, cover)
        for path in not_cached:
            if self._stop_requested:
                return
            try:
                img = None
                if img is None and self.kind != "video":
                    # sin _DISK_LOCK: son pocos KB y no debe esperar a que
                    # el hilo de videos termine de leer un video entero
                    img = read_exif_thumbnail(path)
                    pending.append(path)
                elif img is None:
                    pending.append(path)
                    continue
                self._emit(path, img)
            except Exception:
                continue
        for path in pending:
            if self._stop_requested:
                return
            try:
                with _gen_lock(self.kind):
                    raw = self._load_raw(path)
                self._emit(path, raw)
            except Exception:
                continue
        self.finished_loading.emit()

    def _read_video_frame(self, path):
        """Primer cuadro del video, ya achicado (ffmpeg -> BMP por un pipe)."""
        exe = ffmpeg_exe()
        if not exe:
            return None
        # Achicado en ffmpeg a 2x la cache (despues se recorta con calidad).
        # Un hilo por proceso: corren varios a la vez (_VIDEO_SLOTS).
        # Para que cada uno tarde ~30 % menos (probado con 377 videos de GoPro,
        # imagen identica):
        #   -skip_frame nokey: decodifica solo cuadros clave, el primero lo es
        #   -skip_loop_filter all: sin filtro de desbloqueo
        #   -probesize/-analyzeduration/-fpsprobesize minimos: no decodifica el
        #    primer cuadro dos veces (una para analizar y otra para la imagen)
        scale = f"scale={self.CACHE_W * 2}:-2"
        cmd = [exe, "-hide_banner", "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1",
               "-skip_frame", "nokey", "-skip_loop_filter", "all",
               "-probesize", "32", "-analyzeduration", "0", "-fpsprobesize", "0",
               "-i", path, "-map", "0:v:0", "-frames:v", "1", "-vf", scale,
               "-f", "image2pipe", "-c:v", "bmp", "-"]
        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, timeout=60,
                                 startupinfo=_hidden_startupinfo(), creationflags=_FFMPEG_FLAGS)
        except Exception:
            return None
        if not out.stdout:
            return None
        img = QImage.fromData(out.stdout, "BMP")
        return None if img.isNull() else img


# --- [ DATUMS-TRENNER FUER DIE GALERIE ] ---
#
# Wandelt ein "YYYY-MM-DD"-Datum in eine gut lesbare Ueberschrift um
# ("Heute", "Gestern" oder z.B. "Montag, 14. Juli 2025" / "Monday, July 14,
# 2025"), fuer die Trennzeilen zwischen den einzelnen Tagen in der Video-
# bzw. Bilder-Galerie.
_WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]
_WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]


def format_gallery_date_header(date_str, lang="de"):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return date_str
    today = datetime.now().date()
    if dt.date() == today:
        return tr(lang, "gallery_today")
    if dt.date() == today - timedelta(days=1):
        return tr(lang, "gallery_yesterday")
    if lang == "de":
        return f"{_WEEKDAYS_DE[dt.weekday()]}, {dt.day}. {_MONTHS_DE[dt.month - 1]} {dt.year}"
    if lang == "es":
        return f"{_WEEKDAYS_ES[dt.weekday()].capitalize()}, {dt.day} de {_MONTHS_ES[dt.month - 1]} de {dt.year}"
    return f"{_WEEKDAYS_EN[dt.weekday()]}, {_MONTHS_EN[dt.month - 1]} {dt.day}, {dt.year}"


# --- [ GALERIA VIRTUALIZADA ] ---
#
# Antes: un QLabel + QPixmap por cada archivo (2.700 widgets, ~1,6 GB de RAM a
# 200 %, la interfaz se trababa al cargar y ~1 s al redimensionar). Ahora una
# sola vista dibuja SOLO las filas visibles y pide las miniaturas de lo visible
# (+1 pantalla de margen); guarda las ultimas ~240 en memoria.

class _ThumbIO:
    """Lectura/generacion de miniaturas (cache en disco), sin QThread: reutiliza
    la logica de ThumbnailLoaderThread."""
    CACHE_W, CACHE_H = ThumbnailLoaderThread.CACHE_W, ThumbnailLoaderThread.CACHE_H

    @property
    def CACHE_DIR(self):
        return ThumbnailLoaderThread.CACHE_DIR

    def __init__(self, kind, meta, lang):
        self.kind, self.meta, self.lang = kind, meta or {}, lang

    _cache_path = ThumbnailLoaderThread._cache_path
    _load_cached = ThumbnailLoaderThread._load_cached
    _load_raw = ThumbnailLoaderThread._load_raw
    _read_video_frame = ThumbnailLoaderThread._read_video_frame
    _damaged_image = ThumbnailLoaderThread._damaged_image


class ThumbProvider(QObject):
    """Carga miniaturas bajo demanda en hilos: primero la cache (rapido); si no
    esta, muestra la miniatura EXIF y genera la nitida (fotos de a una con
    _DISK_LOCK para no castigar el disco mecanico; videos de a 4)."""
    ready = Signal(str, QImage)

    def __init__(self, kind, meta, lang, parent=None):
        super().__init__(parent)
        from concurrent.futures import ThreadPoolExecutor
        self.io = _ThumbIO(kind, meta, lang)
        self.kind = kind
        # 6 hilos: hasta 4 pueden estar generando videos (_VIDEO_SLOTS) y los
        # demas siguen sirviendo lo que ya esta en cache sin esperarlos.
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.inflight = set()
        self.generation = 0
        self.dpr = screen_dpr()

    def request(self, paths):
        gen = self.generation
        for path in paths:
            if path not in self.inflight:
                self.inflight.add(path)
                self.pool.submit(self._load, path, gen)

    def _cover(self, img):
        cover = make_cover_thumbnail(img, THUMB_W, THUMB_H, dpr=self.dpr)
        return draw_play_badge(cover) if self.kind == "video" else cover

    def _load(self, path, gen):
        try:
            if gen != self.generation:          # ya se scrolleo a otro lado
                self.inflight.discard(path)
                return
            if self.kind == "video" and is_broken_mp4(path):
                # sin el icono de reproducir: un video danado no se puede abrir
                self.ready.emit(path, make_cover_thumbnail(self.io._damaged_image(), THUMB_W, THUMB_H, dpr=self.dpr))
                return
            img = self.io._load_cached(path)
            if img is None:
                if self.kind != "video":
                    quick = read_exif_thumbnail(path)
                    if quick is not None and gen == self.generation:
                        self.ready.emit(path + "\0quick", self._cover(quick))
                if gen != self.generation:
                    self.inflight.discard(path)
                    return
                with _gen_lock(self.kind):
                    if gen != self.generation:  # se scrolleo mientras esperaba turno
                        self.inflight.discard(path)
                        return
                    img = self.io._load_raw(path)   # genera y guarda en cache
            self.ready.emit(path, self._cover(img) if img is not None and not img.isNull() else QImage())
        except Exception:
            self.ready.emit(path, QImage())

    def shutdown(self):
        self.generation += 1
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


class ThumbGridView(QAbstractScrollArea):
    """Cuadricula virtual: encabezados de fecha + miniaturas, dibujando solo lo
    visible. Senales: activated(ruta) con clic izquierdo, context(ruta, pos)."""
    activated = Signal(str)
    context = Signal(str, QPoint)
    selection_changed = Signal()
    long_pressed = Signal(str)          # mantener apretado (o Ctrl+clic fuera del modo seleccion)
    # HEADER_H: titulo de fecha (34 px de texto) + 10 px libres debajo, donde
    # asoma la "pila" de las rafagas sin taparlo (ver _paint_stack_back)
    SP, HEADER_H, HEADER_TEXT_H = 18, 50, 34
    ROW_H = THUMB_H + 18
    LRU_CAP = 240

    def __init__(self, parent=None):
        super().__init__(parent)
        from collections import OrderedDict
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QAbstractScrollArea { background: transparent; border: none; }")
        self.viewport().setAutoFillBackground(False)
        self.viewport().setMouseTracking(True)
        self.files, self.dates, self.rows, self.row_ys = [], [], [], []
        self.total_h = 0
        self.cols = 3
        self.lang = "es"
        self.lru = OrderedDict()
        self.missing = set()
        self.provider = None
        self.hover = None
        self._press = None
        # Galeria de la camara: estado por elemento ("synced"/"new"), seleccion
        # con un circulo en la esquina y textos de ayuda propios.
        self.status = {}
        self.selectable = False
        self.click_toggles = False      # en modo seleccion de la galeria: clic = elegir / quitar
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(500)
        self._hold_timer.timeout.connect(self._on_hold)
        self._hold_fired = False
        self._press_pos = None
        self.selected = set()
        self.tooltips = {}
        # Videos partidos de la GoPro: ruta -> (parte, total, estado) con estado
        # "ok" (se puede unir), "done" (ya unido) o "bad" (falta/danada una parte)
        self.chapters = {}
        # Secuencias de fotos (time-lapse / rafaga): portada -> (tipo, fotos,
        # expandida, color) y, si esta expandida, cada foto -> (n, total, color)
        self.sequences = {}
        self.seq_frames = {}
        # Videos con recorrido GPS: mapita arriba a la derecha de la miniatura
        self.gps = set()
        self._gps_icon = None
        # Videos con momentos HiLight: ruta -> cantidad (etiqueta amarilla arriba a la izquierda)
        self.hilights = {}
        # Videos: ruta -> duracion ya formateada ("1:05"), abajo a la derecha
        self.durations = {}
        self._dur_font = None
        self.accent = QColor(current_accent(parent))
        self.header_font = QFont("Century Gothic")
        self.header_font.setPixelSize(13)
        self.header_font.setBold(True)
        self.header_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1)
        self.verticalScrollBar().valueChanged.connect(self._schedule_request)
        self._req_timer = QTimer(self)
        self._req_timer.setSingleShot(True)
        self._req_timer.setInterval(0)
        self._req_timer.timeout.connect(self._request_visible)
        # Animacion de carga ("shimmer") en los recuadros que esperan miniatura:
        # 20 cuadros/s, redibuja solo esos recuadros y se apaga sola.
        self._shimmer_phase = 0.0
        self._shimmer_timer = QTimer(self)
        self._shimmer_timer.setInterval(50)
        self._shimmer_timer.timeout.connect(self._shimmer_tick)

    def _pending_rects(self):
        off = self.verticalScrollBar().value()
        x0 = self._x0()
        rects = []
        for y, kind, payload in self._visible_rows():
            if kind != "t":
                continue
            for c, path in enumerate(payload):
                if path not in self.lru and path not in self.missing:
                    rects.append(QRect(x0 + c * (THUMB_W + self.SP), y - off, THUMB_W, THUMB_H))
        return rects

    def _shimmer_tick(self):
        rects = self._pending_rects()
        if not rects or not self.isVisible():
            self._shimmer_timer.stop()
            self.viewport().update()
            return
        self._shimmer_phase = (self._shimmer_phase + 0.06) % 1.6
        for r in rects:
            self.viewport().update(r)

    def _ensure_shimmer(self):
        if not self._shimmer_timer.isActive() and self._pending_rects():
            self._shimmer_timer.start()

    # ---- datos
    def set_items(self, files, dates, meta, kind, lang, provider=None, keep_cache=False):
        if self.provider is not None and self.provider is not provider:
            self.provider.shutdown()
            try:
                self.provider.ready.disconnect(self._on_ready)
            except Exception:
                pass
        same = provider is not None and provider is self.provider
        self.files, self.dates, self.lang = files, dates, lang
        if not keep_cache:
            self.lru.clear()
            self.missing.clear()
        self.hover = None
        if not same:
            self.provider = provider or ThumbProvider(kind, meta, lang, self)
            self.provider.ready.connect(self._on_ready)
        self.verticalScrollBar().setValue(0)
        self._relayout()

    def remove_path(self, path):
        """Quita un archivo de la cuadricula (tras eliminarlo) manteniendo la
        posicion del scroll."""
        if path not in self.files:
            return
        i = self.files.index(path)
        del self.files[i]
        if i < len(self.dates):
            del self.dates[i]
        self.lru.pop(path, None)
        if self.hover == path:
            self.hover = None
        pos = self.verticalScrollBar().value()
        self._relayout()
        self.verticalScrollBar().setValue(min(pos, self.verticalScrollBar().maximum()))

    def loaded_count(self):
        return len(self.lru)

    def _columns(self):
        return max(1, (self.viewport().width() + self.SP) // (THUMB_W + self.SP))

    def _x0(self):
        used = self.cols * (THUMB_W + self.SP) - self.SP
        return max(0, (self.viewport().width() - used) // 2)

    def _relayout(self):
        self.cols = self._columns()
        rows, y, cur, row = [], 0, None, None
        for path, d in zip(self.files, self.dates):
            if d != cur:
                cur = d
                rows.append((y, "h", format_gallery_date_header(d, self.lang)))
                y += self.HEADER_H
                row = None
            if row is None or len(row) >= self.cols:
                row = []
                rows.append((y, "t", row))
                y += self.ROW_H
            row.append(path)
        self.rows, self.total_h = rows, y
        self.row_ys = [r[0] for r in rows]
        self._update_scrollbar()
        self._schedule_request()
        self.viewport().update()

    def _update_scrollbar(self):
        sb = self.verticalScrollBar()
        sb.setRange(0, max(0, self.total_h - self.viewport().height()))
        sb.setPageStep(max(1, self.viewport().height()))
        sb.setSingleStep(60)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.files and self._columns() != self.cols:
            self._relayout()          # solo reacomoda: las miniaturas en memoria se reutilizan
        else:
            self._update_scrollbar()
            self._schedule_request()

    def _visible_rows(self, margin=0):
        import bisect
        top = self.verticalScrollBar().value() - margin
        bottom = self.verticalScrollBar().value() + self.viewport().height() + margin
        i = max(0, bisect.bisect_right(self.row_ys, top) - 1)
        out = []
        while i < len(self.rows) and self.rows[i][0] < bottom:
            out.append(self.rows[i])
            i += 1
        return out

    # ---- pedidos de miniaturas (primero lo visible, despues el margen)
    def _schedule_request(self, *_):
        self._req_timer.start()

    def _request_visible(self):
        if self.provider is None:
            return
        self.provider.generation += 1         # descarta pedidos viejos que no empezaron
        self.provider.inflight.clear()
        visible = [p for _, k, pl in self._visible_rows() if k == "t" for p in pl]
        margin = [p for _, k, pl in self._visible_rows(self.viewport().height()) if k == "t" for p in pl]
        want, seen = [], set()
        for path in visible + margin:
            if path not in seen and path not in self.lru and path not in self.missing:
                seen.add(path)
                want.append(path)
        self.provider.request(want)
        self._ensure_shimmer()

    def _on_ready(self, key, img):
        quick = key.endswith("\0quick")
        path = key[:-6] if quick else key
        if not quick:
            self.provider.inflight.discard(path)
        if img.isNull():
            if not quick:
                self.missing.add(path)
            return
        if quick and path in self.lru:
            return
        self.lru[path] = QPixmap.fromImage(img)
        self.lru.move_to_end(path)
        while len(self.lru) > self.LRU_CAP:
            self.lru.popitem(last=False)
        self.viewport().update()

    # ---- dibujo
    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        off = self.verticalScrollBar().value()
        x0 = self._x0()
        for y, kind, payload in self._visible_rows():
            yy = y - off
            if kind == "h":
                painter.setFont(self.header_font)
                painter.setPen(QColor(255, 255, 255, 140))
                painter.drawText(QRect(x0, yy + 6, self.viewport().width() - x0, self.HEADER_TEXT_H),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, payload)
                continue
            for c, path in enumerate(payload):
                r = QRectF(x0 + c * (THUMB_W + self.SP), yy, THUMB_W, THUMB_H)
                pix = self.lru.get(path)
                clip = QPainterPath()
                clip.addRoundedRect(r, 12, 12)
                sq = self.sequences.get(path)
                if sq and not sq[2]:
                    self._paint_stack_back(painter, r, sq[3])     # "pila" de fotos detras de la portada
                if pix is None:
                    painter.fillPath(clip, QColor(255, 255, 255, 12))
                    if path not in self.missing:
                        # banda de luz que cruza el recuadro mientras carga
                        cx = r.x() + (self._shimmer_phase - 0.3) * r.width()
                        g = QLinearGradient(cx - 90, 0, cx + 90, 0)
                        g.setColorAt(0.0, QColor(255, 255, 255, 0))
                        g.setColorAt(0.5, QColor(255, 255, 255, 42))
                        g.setColorAt(1.0, QColor(255, 255, 255, 0))
                        painter.fillPath(clip, g)
                else:
                    painter.drawPixmap(int(r.x()), int(r.y()), pix)   # ya viene redondeada y a la escala real
                hovered = path == self.hover
                sel = self.selectable and path in self.selected
                if sel:
                    painter.setPen(QPen(self.accent, 3))
                else:
                    painter.setPen(QPen(QColor(255, 255, 255, 220 if hovered else 22), 2 if hovered else 1))
                painter.drawPath(clip)
                st = self.status.get(path)
                if st:
                    self._paint_badge(painter, r, st)
                if path in self.gps:
                    painter.save()
                    self._paint_gps(painter, r)
                    painter.restore()
                n_hl = self.hilights.get(path)
                if n_hl:
                    painter.save()
                    self._paint_hilights(painter, r, n_hl)
                    painter.restore()
                ch = self.chapters.get(path)
                if ch:
                    painter.save()      # que el relleno de la marca no quede para las siguientes miniaturas
                    self._paint_chapter(painter, r, clip, sel, *ch)
                    painter.restore()
                if sq:
                    painter.save()
                    self._paint_sequence(painter, r, clip, sel, *sq)
                    painter.restore()
                dur = self.durations.get(path)
                if dur:
                    painter.save()
                    self._paint_duration(painter, r, dur)
                    painter.restore()
                fr = self.seq_frames.get(path)
                if fr:
                    painter.save()
                    self._paint_seq_frame(painter, r, clip, sel, *fr)
                    painter.restore()
                if self.selectable:
                    self._paint_check(painter, r, sel, hovered)
        painter.end()

    def _paint_stack_back(self, painter, r, color):
        """Dos fotos asomando detras de la portada: se ve que es un grupo."""
        painter.save()
        for k, alpha in ((2, 70), (1, 130)):
            rr = r.translated(5 * k, -5 * k).adjusted(6 * k, 0, -6 * k, 0)
            path = QPainterPath()
            path.addRoundedRect(rr, 12, 12)
            painter.fillPath(path, QColor(40, 42, 54))
            c = QColor(color)
            c.setAlpha(alpha)
            painter.setPen(QPen(c, 1.5))
            painter.drawPath(path)
        painter.restore()

    def _pill(self, painter, rect_left, bottom, text, color, right=None):
        f = QFont(self.header_font)
        f.setPixelSize(11)
        painter.setFont(f)
        w = painter.fontMetrics().horizontalAdvance(text) + 18
        x = rect_left if right is None else right - w
        pill = QRectF(x, bottom - 20, w, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(pill, 10, 10)
        painter.setPen(QColor("#0d0d12"))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        return pill

    def _paint_sequence(self, painter, r, clip, selected, kind, count, expanded, color):
        """Portada de una secuencia: etiqueta TIME-LAPSE · 240 / RAFAGA · 30 y,
        en un time-lapse, el boton de reproducir en el centro."""
        if expanded:
            return                                   # cada foto lleva su propia marca
        key = "seq_badge_burst" if kind == "burst" else "seq_badge_timelapse"
        self._pill(painter, r.left() + 8, r.bottom() - 8, tr(self.lang, key, n=count), color)
        if kind != "burst":
            c = r.center()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 120))
            painter.drawEllipse(c, 22, 22)
            tri = QPainterPath(QPointF(c.x() - 7, c.y() - 11))
            tri.lineTo(c.x() + 12, c.y())
            tri.lineTo(c.x() - 7, c.y() + 11)
            tri.closeSubpath()
            painter.fillPath(tri, QColor(255, 255, 255, 235))

    def _paint_seq_frame(self, painter, r, clip, selected, i, n, color):
        """Foto de una secuencia expandida: borde del color del grupo y n/N."""
        if not selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(color), 3))
            painter.drawPath(clip)
        self._pill(painter, r.left() + 8, r.bottom() - 8, f"{i}/{n}", color)

    def _paint_hilights(self, painter, r, count):
        """Etiqueta "★ 3": el video tiene momentos HiLight (o marcados a mano).
        En modo seleccion se corre a la derecha del circulo de elegir."""
        f = QFont(self.header_font)
        f.setPixelSize(11)
        painter.setFont(f)
        import math
        text = str(count)
        w = painter.fontMetrics().horizontalAdvance(text) + 34
        x = r.left() + (40 if self.selectable else 8)
        pill = QRectF(x, r.top() + 8, w, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffd166"))
        painter.drawRoundedRect(pill, 11, 11)
        # estrella dibujada (Century Gothic no tiene el caracter y buscar otra fuente es lento)
        cx, cy = pill.left() + 13, pill.center().y()
        star = QPainterPath()
        for k in range(10):
            rad = 6.5 if k % 2 == 0 else 2.8
            a = -math.pi / 2 + k * math.pi / 5
            pt = QPointF(cx + rad * math.cos(a), cy + rad * math.sin(a))
            (star.moveTo if k == 0 else star.lineTo)(pt)
        star.closeSubpath()
        painter.fillPath(star, QColor("#0d0d12"))
        painter.setPen(QColor("#0d0d12"))
        painter.drawText(QRectF(pill.left() + 22, pill.top(), w - 26, pill.height()), Qt.AlignmentFlag.AlignCenter, text)

    def _paint_duration(self, painter, r, text):
        """Duracion del video ("1:05") abajo a la derecha, sobre fondo oscuro."""
        if self._dur_font is None:
            self._dur_font = QFont(self.header_font)
            self._dur_font.setPixelSize(11)
            self._dur_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.5)
        painter.setFont(self._dur_font)
        w = painter.fontMetrics().horizontalAdvance(text) + 14
        pill = QRectF(r.right() - w - 8, r.bottom() - 28, w, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(pill, 6, 6)
        painter.setPen(QColor(255, 255, 255, 235))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_gps(self, painter, r):
        """Mapita: el video tiene recorrido GPS (se ve en el reproductor)."""
        if self._gps_icon is None:
            from ..ui.graphics import emoji_icon
            self._gps_icon = emoji_icon("🗺", 16).pixmap(16, 16)
        c = QPointF(r.right() - 20, r.top() + 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 17, 24, 190))
        painter.drawEllipse(c, 14, 14)
        painter.drawPixmap(int(c.x() - 8), int(c.y() - 8), self._gps_icon)

    def _paint_chapter(self, painter, r, clip, selected, part, total, state, color="#00f2fe"):
        """Partes de un video partido: borde y etiqueta del color de su grupo
        (cada video partido tiene el suyo) con PARTE n/N abajo a la izquierda."""
        group = QColor(color)
        if not selected:                     # en modo seleccion manda el borde de elegido
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(group, 3))
            painter.drawPath(clip)
        text = tr(self.lang, "chapter_badge", i=part, n=total)
        if state == "done":
            text += "  ✓"
        elif state == "bad":
            text += "  !"
        f = QFont(self.header_font)
        f.setPixelSize(11)
        painter.setFont(f)
        w = painter.fontMetrics().horizontalAdvance(text) + 18
        pill = QRectF(r.left() + 8, r.bottom() - 28, w, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(group)
        fg = QColor("#0d0d12")
        painter.drawRoundedRect(pill, 10, 10)
        painter.setPen(fg)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_badge(self, painter, r, status):
        """Marca de estado: check verde = ya sincronizado; NUEVO = falta copiar."""
        if status == "synced":
            c = QPointF(r.right() - 18, r.top() + 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 110))
            painter.drawEllipse(c, 13, 13)
            painter.setBrush(QColor("#2ecc71"))
            painter.drawEllipse(c, 11, 11)
            pen = QPen(QColor("#ffffff"), 2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath(QPointF(c.x() - 5, c.y()))
            path.lineTo(c.x() - 1.5, c.y() + 3.5)
            path.lineTo(c.x() + 5, c.y() - 4)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        else:
            text = tr(self.lang, "cam_badge_new")
            f = QFont(self.header_font)
            f.setPixelSize(11)
            painter.setFont(f)
            w = painter.fontMetrics().horizontalAdvance(text) + 16
            pill = QRectF(r.right() - w - 8, r.top() + 8, w, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.accent)
            painter.drawRoundedRect(pill, 10, 10)
            painter.setPen(QColor("#0d0d12"))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

    def _check_rect(self, r):
        return QRectF(r.left() + 8, r.top() + 8, 24, 24)

    def _paint_check(self, painter, r, selected, hovered):
        cr = self._check_rect(r)
        painter.setPen(QPen(QColor(255, 255, 255, 230 if (selected or hovered) else 150), 2))
        painter.setBrush(self.accent if selected else QColor(0, 0, 0, 90))
        painter.drawEllipse(cr)
        if selected:
            pen = QPen(QColor("#0d0d12"), 2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            c = cr.center()
            path = QPainterPath(QPointF(c.x() - 5, c.y()))
            path.lineTo(c.x() - 1.5, c.y() + 3.5)
            path.lineTo(c.x() + 5, c.y() - 4)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    def _cell_rect(self, path):
        off = self.verticalScrollBar().value()
        x0 = self._x0()
        for y, kind, payload in self._visible_rows():
            if kind == "t" and path in payload:
                c = payload.index(path)
                return QRectF(x0 + c * (THUMB_W + self.SP), y - off, THUMB_W, THUMB_H)
        return None

    def toggle_selected(self, path):
        if path in self.selected:
            self.selected.discard(path)
        else:
            self.selected.add(path)
        self.viewport().update()
        self.selection_changed.emit()

    # ---- interaccion
    def path_at(self, pos):
        off = self.verticalScrollBar().value()
        x0 = self._x0()
        for y, kind, payload in self._visible_rows():
            if kind == "t" and y - off <= pos.y() < y - off + THUMB_H:
                rel = pos.x() - x0
                c = rel // (THUMB_W + self.SP)
                if rel >= 0 and 0 <= c < len(payload) and rel % (THUMB_W + self.SP) < THUMB_W:
                    return payload[int(c)]
        return None

    def mouseMoveEvent(self, event):
        h = self.path_at(event.position().toPoint())
        if h != self.hover:
            self.hover = h
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if h else Qt.CursorShape.ArrowCursor)
            self.viewport().update()

    def leaveEvent(self, event):
        if self.hover is not None:
            self.hover = None
            self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._press = self.path_at(event.position().toPoint()) if event.button() == Qt.MouseButton.LeftButton else None
        self._hold_fired = False
        self._press_pos = event.position().toPoint()
        if self._press:
            self._hold_timer.start()
        super().mousePressEvent(event)

    def _on_hold(self):
        if self._press:
            self._hold_fired = True
            if self.selectable:
                self.toggle_selected(self._press)
            else:
                self.long_pressed.emit(self._press)

    def mouseReleaseEvent(self, event):
        self._hold_timer.stop()
        if event.button() == Qt.MouseButton.LeftButton and not self._hold_fired:
            pos = event.position().toPoint()
            path = self.path_at(pos)
            if path and path == self._press:
                cell = self._cell_rect(path)
                ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                on_circle = cell is not None and self._check_rect(cell).contains(QPointF(pos))
                if self.selectable and (ctrl or on_circle or self.click_toggles):
                    self.toggle_selected(path)      # circulo, Ctrl+clic o modo seleccion: elegir
                elif ctrl:
                    self.long_pressed.emit(path)    # Ctrl+clic entra en modo seleccion
                else:
                    self.activated.emit(path)
        self._press = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        path = self.path_at(event.pos())
        if path:
            self.context.emit(path, event.globalPos())

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip:
            path = self.path_at(event.pos())
            if path:
                QToolTip.showText(event.globalPos(), self.tooltips.get(path, path), self.viewport())
            else:
                QToolTip.hideText()
            return True
        return super().viewportEvent(event)

    def shutdown(self):
        if self.provider is not None:
            self.provider.shutdown()


class _InflightUnion:
    """inflight de dos proveedores, para que la grilla lo maneje como uno."""

    def __init__(self, *providers):
        self.providers = providers

    def clear(self):
        for pr in self.providers:
            pr.inflight.clear()

    def discard(self, key):
        for pr in self.providers:
            pr.inflight.discard(key)

    def __contains__(self, key):
        return any(key in pr.inflight for pr in self.providers)


class MixedThumbProvider(QObject):
    """Miniaturas de fotos y videos mezclados (una tarjeta SD): cada archivo
    va al proveedor que corresponde."""
    ready = Signal(str, QImage)

    def __init__(self, lang, parent=None):
        super().__init__(parent)
        self.pics = ThumbProvider("picture", {}, lang, self)
        self.vids = ThumbProvider("video", {}, lang, self)
        self.pics.ready.connect(self.ready)
        self.vids.ready.connect(self.ready)
        self.inflight = _InflightUnion(self.pics, self.vids)
        self._gen = 0

    @property
    def generation(self):
        return self._gen

    @generation.setter
    def generation(self, value):
        self._gen = value
        self.pics.generation += 1
        self.vids.generation += 1

    def request(self, paths):
        vexts = tuple(MEDIA_EXTENSIONS_BY_MODE["videos"])
        self.vids.request([x for x in paths if x.lower().endswith(vexts)])
        self.pics.request([x for x in paths if not x.lower().endswith(vexts)])

    def shutdown(self):
        self.pics.shutdown()
        self.vids.shutdown()
