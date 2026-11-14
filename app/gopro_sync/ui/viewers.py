"""Reproductor de video y visor de fotos."""
import os
from PySide6.QtCore import QPointF, QRectF, QSize, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider,
                               QSplitter, QStackedWidget, QVBoxLayout, QWidget)
from ..env import FONT_STACK
from ..i18n import tr
from ..sound import SoundManager
from ..media import _read_moov, read_gopro_track
from ..workers import ScanThread
from ..ui.graphics import emoji_icon
from ..ui.widgets import VideoFrameView, save_captured_photo, show_toast

def fullscreen_icon(px=18, leave=False):
    """Icono de pantalla completa dibujado (las 4 esquinas): sin depender de
    una fuente de emojis."""
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#ffffff"), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    m, L, s = 2.5, 5.5, px - 2.5
    for cx, cy, dx, dy in ((m, m, 1, 1), (s, m, -1, 1), (m, s, 1, -1), (s, s, -1, -1)):
        if leave:                     # salir: esquinas hacia adentro
            cx, cy, dx, dy = cx + dx * L, cy + dy * L, -dx, -dy
        p.drawLine(QPointF(cx, cy), QPointF(cx + dx * L, cy))
        p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * L))
    p.end()
    return QIcon(pm)


def toggle_fullscreen(window):
    if window.isFullScreen():
        window.showNormal()
    else:
        window.showFullScreen()


class VideoPlayerDialog(QDialog):
    """Eigener Video-Player mit richtiger Steuerleiste (Play/Pause, Zeit-
    Anzeige, Sucher-Slider, Lautstaerke) - oeffnet sich beim Anklicken eines
    Videos in der Video-Galerie.

    WICHTIG (Performance): normale Videos laufen ueber das ganz normale,
    hardwarebeschleunigte QVideoWidget - genau wie vorher. Die CPU-seitige
    Frame-Konvertierung (QVideoSink -> QImage) wird NUR eingeschaltet,
    waehrend der 360-Grad-Modus tatsaechlich aktiv ist (das ist unvermeidbar,
    weil wir dafuer selbst in die Pixel eingreifen muessen, um den Ausschnitt
    zu berechnen). Fuer den 📷-Knopf im normalen Modus wird nicht dauerhaft
    mitgeschnitten, sondern nur genau EIN Frame kurz abgegriffen, wenn
    tatsaechlich auf den Knopf gedrueckt wird.

    Bei equirektangulaeren Videos (Seitenverhaeltnis ~2:1) wird automatisch
    (anhand der Aufloesung aus den Metadaten, ohne einen Frame dekodieren zu
    muessen) in den 360-Modus gewechselt; der 🌐-Knopf schaltet jederzeit
    manuell um."""
    def __init__(self, path, gallery_dir=None, lang="de", parent=None):
        super().__init__(parent)
        self.gallery_dir = gallery_dir
        self.lang = lang
        self._auto_mode_detected = False
        self.view_mode = "flat"
        self._capture_sink = None
        self.setWindowTitle(os.path.basename(path))
        self.resize(960, 600)
        self.setStyleSheet(f"QDialog {{ background: #000; }} * {{ color: #fff; font-family: {FONT_STACK}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.video_widget = QVideoWidget()      # normale Wiedergabe (hardwarebeschleunigt)
        self.pan_view = VideoFrameView(radius=0)  # nur fuer 360-Modus
        self.stack.addWidget(self.video_widget)
        self.stack.addWidget(self.pan_view)
        # Video a la izquierda; el mapa del recorrido (si el video tiene GPS) a la derecha
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setStyleSheet("QSplitter::handle { background: #222; width: 3px; }")
        self.split.addWidget(self.stack)
        self.map_panel = None
        self._track = None
        layout.addWidget(self.split, 1)
        self.video_widget.mouseDoubleClickEvent = lambda e: self.toggle_fullscreen()

        controls = QWidget()
        controls.setStyleSheet("background: #15151c;")
        c_layout = QHBoxLayout(controls)
        c_layout.setContentsMargins(16, 10, 16, 10)
        c_layout.setSpacing(12)

        def _round_btn(ch, tooltip=""):
            # Emoji como icono (una sola fuente): como texto, la primera vez
            # Windows buscaba la fuente en todo el sistema y el reproductor
            # tardaba ~3 s en aparecer.
            btn = QPushButton()
            btn.setIcon(QIcon(emoji_icon(ch, 18)))
            btn.setIconSize(QSize(18, 18))
            btn.setFixedSize(38, 38)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tooltip)
            btn.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.1); border-radius: 19px; font-size: 15px; border: none; } "
                "QPushButton:hover { background: rgba(255,255,255,0.2); } "
                "QPushButton:checked { background: rgba(0,242,254,0.35); }"
            )
            return btn

        self.btn_play = _round_btn("⏸")
        self.btn_play.clicked.connect(self.toggle_play)
        c_layout.addWidget(self.btn_play)

        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.7); background: transparent;")
        c_layout.addWidget(self.lbl_time)

        self.slider_seek = QSlider(Qt.Orientation.Horizontal)
        self.slider_seek.setRange(0, 0)
        self.slider_seek.sliderMoved.connect(self.on_seek)
        c_layout.addWidget(self.slider_seek, 1)

        vol_icon = QLabel()
        vol_icon.setPixmap(emoji_icon("🔊", 18).pixmap(18, 18))
        vol_icon.setStyleSheet("background: transparent;")
        c_layout.addWidget(vol_icon)
        self.slider_volume = QSlider(Qt.Orientation.Horizontal)
        self.slider_volume.setFixedWidth(110)
        self.slider_volume.setRange(0, 100)
        self.slider_volume.setValue(80)
        self.slider_volume.valueChanged.connect(self.on_volume)
        c_layout.addWidget(self.slider_volume)

        self.btn_360 = _round_btn("🌐", tr(self.lang, "view_360"))
        self.btn_360.setCheckable(True)
        self.btn_360.clicked.connect(self.toggle_360_mode)
        c_layout.addWidget(self.btn_360)

        self.btn_capture = _round_btn("📷", tr(self.lang, "capture_frame"))
        self.btn_capture.clicked.connect(self.capture_frame)
        c_layout.addWidget(self.btn_capture)

        # Mapa: aparece solo si el video tiene GPS (se lee en segundo plano)
        self.btn_map = _round_btn("🗺", tr(self.lang, "map_toggle"))
        self.btn_map.setCheckable(True)
        self.btn_map.hide()
        self.btn_map.clicked.connect(self.toggle_map)
        c_layout.addWidget(self.btn_map)

        self.btn_full = _round_btn("📷", tr(self.lang, "fullscreen_toggle"))
        self.btn_full.setIcon(fullscreen_icon())
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        c_layout.addWidget(self.btn_full)

        layout.addWidget(controls)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)

        self.pan_sink = QVideoSink(self)
        self.pan_sink.videoFrameChanged.connect(self._on_pan_frame)

        # Standardmaessig ganz normal auf das Video-Widget ausgeben -
        # exakt wie vor dem 360-Feature, also wieder mit voller Performance.
        self.player.setVideoOutput(self.video_widget)

        self.player.metaDataChanged.connect(self._on_metadata_changed)
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        # Abrir el video bloqueaba la ventana ~1-3 s antes de mostrarse (Qt lee
        # el indice, que en la GoPro esta al FINAL del archivo, en el disco).
        # Ahora: la ventana aparece ya con "Cargando video...", el indice se
        # lee en segundo plano (queda en la memoria de Windows) y recien
        # despues se abre el video.
        self._path = path
        self.loading_lbl = QLabel(tr(lang, "player_loading"), self.stack)
        self.loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_lbl.setStyleSheet("color: rgba(255,255,255,0.75); font-size: 16px; background: transparent;")
        QTimer.singleShot(60, self._start_loading)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "loading_lbl", None) is not None:
            self.loading_lbl.setGeometry(self.stack.rect())

    def _start_loading(self):
        self.loading_lbl.setGeometry(self.stack.rect())
        self.loading_lbl.raise_()
        th = ScanThread(0, lambda: _read_moov(self._path))
        th.done.connect(lambda _t, _r: self._open_media())
        th.start()
        gps = ScanThread(0, read_gopro_track, self._path)
        gps.done.connect(self._on_track)
        gps.start()

    def _on_track(self, _t, track):
        if track and track.get("points"):
            self._track = track
            self.btn_map.show()

    def toggle_map(self):
        SoundManager.play_click()
        if self.map_panel is None and self._track:
            from ..ui.map import TileMapWidget
            accent = getattr(self.window().parent(), "get_current_theme_color", lambda: "#00f2fe")() \
                if self.parent() is not None else "#00f2fe"
            self.map_panel = TileMapWidget(self._track["points"], accent, self)
            self.map_panel.setMinimumSize(320, 240)
            self.split.addWidget(self.map_panel)
            self.split.setSizes([max(400, self.width() - 420), 420])
        if self.map_panel is not None:
            self.map_panel.setVisible(self.btn_map.isChecked())

    def toggle_fullscreen(self):
        toggle_fullscreen(self)
        self.btn_full.setIcon(fullscreen_icon(leave=self.isFullScreen()))

    def _open_media(self):
        if not self.isVisible():
            return
        self.player.setSource(QUrl.fromLocalFile(self._path))
        self.player.play()

    def _on_media_status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                      QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.EndOfMedia):
            self.loading_lbl.hide()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.loading_lbl.setText(tr(self.lang, "video_broken_msg", name=os.path.basename(self._path)))
            self.loading_lbl.show()

    def _on_metadata_changed(self):
        # Erkennt 360-Grad-Videos anhand der Aufloesung aus den Metadaten -
        # OHNE dafuer einen einzigen Frame dekodieren zu muessen. Deutlich
        # billiger als das dauerhafte Mitschneiden ueber einen QVideoSink.
        if self._auto_mode_detected:
            return
        try:
            meta = self.player.metaData()
            size = meta.value(QMediaMetaData.Key.Resolution)
        except Exception:
            return
        if not size or not size.isValid() or size.height() <= 0:
            return
        self._auto_mode_detected = True
        ratio = size.width() / size.height()
        # equirektangulaere 360-Videos sind ca. 2:1
        if 1.85 <= ratio <= 2.15:
            self.set_360_mode(True)

    def set_360_mode(self, enabled):
        self.view_mode = "360" if enabled else "flat"
        self.btn_360.setChecked(enabled)
        if enabled:
            self.pan_view.set_mode("360")
            self.stack.setCurrentWidget(self.pan_view)
            self.player.setVideoOutput(self.pan_sink)
        else:
            self.stack.setCurrentWidget(self.video_widget)
            self.player.setVideoOutput(self.video_widget)

    def toggle_360_mode(self):
        SoundManager.play_click()
        self.set_360_mode(self.btn_360.isChecked())

    def _on_pan_frame(self, frame):
        # Wird NUR aufgerufen, waehrend der 360-Modus aktiv ist (siehe
        # set_360_mode) - im normalen Modus laeuft ueberhaupt kein Sink mit.
        if not frame.isValid():
            return
        img = frame.toImage()
        if not img.isNull():
            self.pan_view.set_frame(img)

    def capture_frame(self):
        SoundManager.play_click()
        if self.view_mode == "360":
            img = self.pan_view.capture_native()
            self._finish_capture(img)
            return
        # Normaler Modus: es haengt bewusst kein Sink permanent mit (Performance!),
        # daher hier kurz einen Sink andocken, GENAU EIN Frame abgreifen und
        # danach sofort wieder zurueck auf das normale Video-Widget schalten.
        if self._capture_sink is None:
            self._capture_sink = QVideoSink(self)

        def _on_single_frame(frame):
            try:
                self._capture_sink.videoFrameChanged.disconnect(_on_single_frame)
            except Exception:
                pass
            self.player.setVideoOutput(self.video_widget)
            img = None
            if frame.isValid():
                candidate = frame.toImage()
                if not candidate.isNull():
                    img = candidate
            self._finish_capture(img)

        self._capture_sink.videoFrameChanged.connect(_on_single_frame)
        self.player.setVideoOutput(self._capture_sink)

    def _finish_capture(self, img):
        if img is None or img.isNull():
            return
        target_dir = self.gallery_dir or os.path.expanduser("~/Pictures")
        saved_path = save_captured_photo(target_dir, img)
        if saved_path:
            show_toast(self, tr(self.lang, "saved_in_gallery"))

    def toggle_play(self):
        SoundManager.play_click()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play.setIcon(QIcon(emoji_icon("▶", 18)))
        else:
            self.player.play()
            self.btn_play.setIcon(QIcon(emoji_icon("⏸", 18)))

    def on_seek(self, value):
        self.player.setPosition(value)

    def on_volume(self, value):
        self.audio.setVolume(value / 100.0)

    def on_position_changed(self, pos):
        if not self.slider_seek.isSliderDown():
            self.slider_seek.setValue(pos)
        if self.map_panel is not None and self.map_panel.isVisible() and self.player.duration():
            self.map_panel.set_progress(pos / self.player.duration())
        self._update_time_label(pos, self.player.duration())

    def on_duration_changed(self, dur):
        self.slider_seek.setRange(0, dur)
        self._update_time_label(self.player.position(), dur)

    def _update_time_label(self, pos, dur):
        def fmt(ms):
            s = max(0, ms) // 1000
            return f"{s // 60:02d}:{s % 60:02d}"
        self.lbl_time.setText(f"{fmt(pos)} / {fmt(dur)}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.toggle_play()
        elif event.key() in (Qt.Key.Key_F11, Qt.Key.Key_F):
            self.toggle_fullscreen()
        elif event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()          # Esc sale de pantalla completa (no cierra)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.player.stop()
        if self.map_panel is not None:
            self.map_panel.shutdown()
        super().closeEvent(event)


class ZoomableGraphicsView(QGraphicsView):
    """QGraphicsView mit Mausrad-Zoom (Verschieben durch Ziehen ist ueber
    ScrollHandDrag bereits eingebaut)."""
    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


class ImageViewerDialog(QDialog):
    """Vollbild-Bildbetrachter mit Zoom (Mausrad) und Verschieben (Ziehen) -
    oeffnet sich beim Anklicken eines Bildes in der Bilder-Galerie.
    Doppelklick setzt den Zoom wieder auf "Bild einpassen" zurueck."""
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(os.path.basename(path))
        self.resize(1000, 700)
        self.setStyleSheet("background: #000;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = ZoomableGraphicsView()
        self.view.setStyleSheet("border: none; background: #000;")
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        scene = QGraphicsScene(self)
        pix = QPixmap(path)
        self.pixmap_item = scene.addPixmap(pix)
        scene.setSceneRect(QRectF(pix.rect()))
        self.view.setScene(scene)
        self.view.mouseDoubleClickEvent = lambda event: self._fit_to_view()
        layout.addWidget(self.view)

        # Boton de pantalla completa sobre la foto (arriba a la derecha); F11 o F tambien.
        self.btn_full = QPushButton(self)
        self.btn_full.setIcon(fullscreen_icon())
        self.btn_full.setIconSize(QSize(18, 18))
        self.btn_full.setFixedSize(38, 38)
        self.btn_full.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_full.setStyleSheet("QPushButton { background: rgba(0,0,0,0.55); border-radius: 19px; border: none; }"
                                    " QPushButton:hover { background: rgba(255,255,255,0.2); }")
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        QTimer.singleShot(0, self._fit_to_view)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.btn_full.move(self.width() - 50, 12)
        self.btn_full.raise_()
        QTimer.singleShot(0, self._fit_to_view)

    def toggle_fullscreen(self):
        toggle_fullscreen(self)
        self.btn_full.setIcon(fullscreen_icon(leave=self.isFullScreen()))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_F11, Qt.Key.Key_F):
            self.toggle_fullscreen()
        elif event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(event)

    def _fit_to_view(self):
        self.view.resetTransform()
        self.view.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)


class ClickableThumbnail(QLabel):
    def __init__(self, pixmap, on_click, tooltip="", parent=None, on_context=None):
        super().__init__(parent)
        self._on_context = on_context
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.deviceIndependentSize().toSize())
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip: self.setToolTip(tooltip)
        self._on_click = on_click
        # El estilo (borde y hover) lo define la galeria UNA vez para todas
        # las miniaturas: con miles, un setStyleSheet por miniatura era lento.
        # Sin QGraphicsDropShadowEffect: con miles de miniaturas cada sombra
        # se recalculaba en cada repintado y volvia lenta toda la galeria.

    def mousePressEvent(self, event):
        # Solo el clic izquierdo abre (el derecho muestra el menu contextual)
        if event.button() == Qt.MouseButton.LeftButton and self._on_click:
            self._on_click()
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        if self._on_context:
            self._on_context(event.globalPos())
