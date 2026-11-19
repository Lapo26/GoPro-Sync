"""Reproductor de video y visor de fotos."""
import os
from PySide6.QtCore import QPointF, QRectF, QSize, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QIcon, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider,
                               QSplitter, QStackedWidget, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget)
from ..env import FONT_STACK, load_config
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


def _player_audio_defaults(parent):
    """(volumen 0-100, silenciado) por defecto del reproductor, de Ajustes.
    Usa la configuracion ya cargada en la ventana principal; si no la
    encuentra, la lee del disco."""
    cfg, w = None, parent
    while w is not None:
        if isinstance(getattr(w, "config", None), dict):
            cfg = w.config
            break
        w = w.parent()
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
    try:
        vol = int(cfg.get("player_volume", 80))
    except (TypeError, ValueError):
        vol = 80
    return max(0, min(100, vol)), bool(cfg.get("player_start_muted", False))


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
    def __init__(self, path, gallery_dir=None, lang="de", parent=None, playlist=None):
        super().__init__(parent)
        self.gallery_dir = gallery_dir
        self.lang = lang
        # anterior / siguiente: los videos de la galeria en el mismo orden
        self.playlist = [x for x in (playlist or []) if x]
        if path not in self.playlist:
            self.playlist = [path]
        self._load_gen = 0
        self._clip_in = self._clip_out = None
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
                "QPushButton:checked { background: rgba(0,242,254,0.35); } "
                "QPushButton:disabled { background: rgba(255,255,255,0.04); }"
            )
            return btn

        self.btn_prev = _round_btn("⏮", tr(self.lang, "nav_prev_video"))
        self.btn_prev.clicked.connect(lambda: self.step_media(-1))
        c_layout.addWidget(self.btn_prev)

        self.btn_play = _round_btn("⏸")
        self.btn_play.clicked.connect(self.toggle_play)
        c_layout.addWidget(self.btn_play)

        self.btn_next = _round_btn("⏭", tr(self.lang, "nav_next_video"))
        self.btn_next.clicked.connect(lambda: self.step_media(1))
        c_layout.addWidget(self.btn_next)
        for b in (self.btn_prev, self.btn_next):
            b.setVisible(len(self.playlist) > 1)

        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.7); background: transparent;")
        c_layout.addWidget(self.lbl_time)

        self.slider_seek = SeekSlider(Qt.Orientation.Horizontal)
        self.slider_seek.setRange(0, 0)
        self.slider_seek.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # las flechas van al reproductor (5 s), no de a 1 ms
        self.slider_seek.sliderMoved.connect(self.on_seek)
        self.slider_seek.sliderReleased.connect(lambda: self._do_seek(self.slider_seek.value()))
        # Al arrastrar se pedia un salto por cada pixel y el video 4K se
        # quedaba atrasado procesando saltos viejos: se agrupan cada 150 ms.
        self._seek_target = None
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(150)
        self._seek_timer.timeout.connect(lambda: self._do_seek(self._seek_target))
        c_layout.addWidget(self.slider_seek, 1)

        # Volumen y silencio: arrancan con lo elegido en Ajustes > Sonidos.
        # Lo que se cambie aca vale para esta ventana (tambien al pasar al
        # video siguiente) y no toca el valor por defecto.
        vol, muted = _player_audio_defaults(parent)
        self.btn_mute = _round_btn("🔊", tr(self.lang, "player_mute"))
        self.btn_mute.setCheckable(True)
        self.btn_mute.clicked.connect(self.toggle_mute)
        c_layout.addWidget(self.btn_mute)
        self.slider_volume = QSlider(Qt.Orientation.Horizontal)
        self.slider_volume.setFixedWidth(110)
        self.slider_volume.setRange(0, 100)
        self.slider_volume.setValue(vol)
        self.slider_volume.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # las flechas siguen siendo del reproductor
        self.slider_volume.valueChanged.connect(self.on_volume)
        c_layout.addWidget(self.slider_volume)

        self.btn_360 = _round_btn("🌐", tr(self.lang, "view_360"))
        self.btn_360.setCheckable(True)
        self.btn_360.clicked.connect(self.toggle_360_mode)
        c_layout.addWidget(self.btn_360)

        self.btn_capture = _round_btn("📷", tr(self.lang, "capture_frame"))
        self.btn_capture.clicked.connect(self.capture_frame)
        c_layout.addWidget(self.btn_capture)

        self.btn_clip = _round_btn("✂", tr(self.lang, "clip_toggle"))
        self.btn_clip.setCheckable(True)
        self.btn_clip.clicked.connect(self.toggle_clip_bar)
        c_layout.addWidget(self.btn_clip)

        # Mapa: siempre visible; gris mientras se lee el GPS (en segundo plano)
        # o si el video no tiene recorrido, con el motivo en el tooltip.
        self.btn_map = _round_btn("🗺", tr(self.lang, "gps_reading"))
        self.btn_map.setCheckable(True)
        self.btn_map.setEnabled(False)
        self.btn_map.clicked.connect(self.toggle_map)
        c_layout.addWidget(self.btn_map)

        self.btn_full = _round_btn("📷", tr(self.lang, "fullscreen_toggle"))
        self.btn_full.setIcon(fullscreen_icon())
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        c_layout.addWidget(self.btn_full)

        # Barra para recortar un fragmento (se abre con las tijeras o con I / O)
        self.clip_bar = QWidget()
        self.clip_bar.setStyleSheet(
            "QWidget { background: #1c1c26; } QLabel { background: transparent; font-size: 12px; }"
            " QPushButton { background: rgba(255,255,255,0.1); border: none; border-radius: 12px; padding: 6px 14px;"
            " font-size: 12px; font-weight: 600; } QPushButton:hover { background: rgba(255,255,255,0.2); }"
            " QPushButton:disabled { color: rgba(255,255,255,0.35); background: rgba(255,255,255,0.04); }"
            " QPushButton#primary { background: #00b4d8; color: #0d0d12; font-weight: 800; }"
            " QPushButton#primary:hover { background: #22c8ea; }"
            " QPushButton#primary:disabled { background: rgba(0,180,216,0.25); color: rgba(13,13,18,0.6); }")
        cb = QHBoxLayout(self.clip_bar)
        cb.setContentsMargins(16, 8, 16, 8)
        cb.setSpacing(10)
        self.btn_in = QPushButton(tr(self.lang, "clip_mark_in"))
        self.btn_in.clicked.connect(self.mark_in)
        self.btn_out = QPushButton(tr(self.lang, "clip_mark_out"))
        self.btn_out.clicked.connect(self.mark_out)
        self.btn_play_clip = QPushButton(tr(self.lang, "clip_preview"))
        self.btn_play_clip.clicked.connect(self.preview_clip)
        self.lbl_clip = QLabel(tr(self.lang, "clip_hint"))
        self.lbl_clip.setStyleSheet("color: rgba(255,255,255,0.75);")
        self.btn_save_clip = QPushButton(tr(self.lang, "clip_save"))
        self.btn_save_clip.setObjectName("primary")
        self.btn_save_clip.clicked.connect(self.save_clip)
        for w in (self.btn_in, self.btn_out, self.btn_play_clip):
            cb.addWidget(w)
        cb.addWidget(self.lbl_clip, 1)
        cb.addWidget(self.btn_save_clip)
        self.clip_bar.hide()
        layout.addWidget(controls)
        layout.addWidget(self.clip_bar)
        self._update_clip_ui()
        self._clip_stop_at = None

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(vol / 100.0)
        self.player.setAudioOutput(self.audio)
        self.set_muted(muted)

        self.pan_sink = QVideoSink(self)
        self.pan_sink.videoFrameChanged.connect(self._on_pan_frame)

        # Standardmaessig ganz normal auf das Video-Widget ausgeben -
        # exakt wie vor dem 360-Feature, also wieder mit voller Performance.
        self.player.setVideoOutput(self.video_widget)

        self.player.metaDataChanged.connect(self._on_metadata_changed)
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        # Momentos HiLight (los de la camara y los marcados a mano)
        from ..ui.hilights import HiLightExtras
        self.hilight_extras = HiLightExtras(self, c_layout, _round_btn)
        # Grafico de velocidad y altura debajo del video (boton o tecla G)
        from ..ui.telemetry_panel import TelemetryExtras
        self.telemetry_extras = TelemetryExtras(self, c_layout, layout, _round_btn)
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
        gen, path = self._load_gen, self._path
        th = ScanThread(gen, lambda: _read_moov(path))
        th.done.connect(lambda t, _r: self._open_media() if t == self._load_gen else None)
        th.start()
        gps = ScanThread(gen, read_gopro_track, path)
        gps.done.connect(self._on_track)
        gps.start()

    # ---- anterior / siguiente
    def step_media(self, delta):
        if len(self.playlist) < 2:
            return
        try:
            i = self.playlist.index(self._path)
        except ValueError:
            i = 0
        j = i + delta
        if not 0 <= j < len(self.playlist):
            show_toast(self, tr(self.lang, "nav_last" if delta > 0 else "nav_first"))
            return
        SoundManager.play_click()
        self.load_path(self.playlist[j])

    def load_path(self, path):
        """Cambia de video sin cerrar el reproductor."""
        self._load_gen += 1
        self.player.stop()
        self.player.setSource(QUrl())
        self._path = path
        self.setWindowTitle(os.path.basename(path))
        self._auto_mode_detected = False
        if self.view_mode == "360":
            self.set_360_mode(False)
        if self.map_panel is not None:
            self.map_panel.shutdown()
            self.map_panel.hide()
            self.map_panel.setParent(None)
            self.map_panel.deleteLater()
            self.map_panel = None
        self._track = None
        self.btn_map.setChecked(False)
        self.btn_map.setEnabled(False)
        self.btn_map.setToolTip(tr(self.lang, "gps_reading"))
        self._clip_in = self._clip_out = None
        self._clip_stop_at = None
        self._update_clip_ui()
        self.slider_seek.setRange(0, 0)
        self._update_time_label(0, 0)
        self.btn_play.setIcon(QIcon(emoji_icon("⏸", 18)))
        self._update_nav_buttons()
        self.loading_lbl.setText(tr(self.lang, "player_loading"))
        self.loading_lbl.show()
        self._start_loading()

    def _update_nav_buttons(self):
        try:
            i = self.playlist.index(self._path)
        except ValueError:
            i = 0
        self.btn_prev.setEnabled(i > 0)
        self.btn_next.setEnabled(i < len(self.playlist) - 1)
        if len(self.playlist) > 1:
            self.btn_prev.setToolTip(tr(self.lang, "nav_prev_video") + f"  ({i + 1}/{len(self.playlist)})")
            self.btn_next.setToolTip(tr(self.lang, "nav_next_video") + f"  ({i + 1}/{len(self.playlist)})")

    # ---- recortar un fragmento
    def toggle_clip_bar(self):
        SoundManager.play_click()
        self.clip_bar.setVisible(self.btn_clip.isChecked())
        if not self.clip_bar.isVisible():
            self._clip_in = self._clip_out = None
            self._update_clip_ui()

    def _show_clip_bar(self):
        if not self.clip_bar.isVisible():
            self.btn_clip.setChecked(True)
            self.clip_bar.show()

    def mark_in(self):
        self._show_clip_bar()
        self._clip_in = self.player.position()
        if self._clip_out is not None and self._clip_out <= self._clip_in:
            self._clip_out = None
        self._update_clip_ui()

    def mark_out(self):
        self._show_clip_bar()
        pos = self.player.position()
        if self._clip_in is None:
            self._clip_in = 0
        if pos <= self._clip_in:
            show_toast(self, tr(self.lang, "clip_out_before_in"))
            return
        self._clip_out = pos
        self._update_clip_ui()

    def _clip_range(self):
        if self._clip_in is None:
            return None
        end = self._clip_out if self._clip_out is not None else self.player.duration()
        if not end or end <= self._clip_in:
            return None
        return self._clip_in, end

    def _update_clip_ui(self):
        def fmt(ms):
            s = max(0, int(ms)) // 1000
            return f"{s // 60:02d}:{s % 60:02d}"
        rng = self._clip_range() if hasattr(self, "player") else None
        if self._clip_in is None:
            self.lbl_clip.setText(tr(self.lang, "clip_hint"))
        elif rng is None:
            self.lbl_clip.setText(tr(self.lang, "clip_from", a=fmt(self._clip_in)))
        else:
            a, b = rng
            self.lbl_clip.setText(tr(self.lang, "clip_range", a=fmt(a), b=fmt(b), d=fmt(b - a)))
        self.btn_save_clip.setEnabled(rng is not None)
        self.btn_play_clip.setEnabled(rng is not None)
        self.slider_seek.mark_in = self._clip_in
        self.slider_seek.mark_out = rng[1] if rng else None
        self.slider_seek.update()

    def preview_clip(self):
        rng = self._clip_range()
        if not rng:
            return
        self._clip_stop_at = rng[1]
        self.slider_seek.setValue(rng[0])
        self._do_seek(rng[0])
        self.player.play()
        self.btn_play.setIcon(QIcon(emoji_icon("⏸", 18)))

    def save_clip(self):
        rng = self._clip_range()
        if not rng:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: #23232b; border: 1px solid rgba(255,255,255,0.12); padding: 6px; }"
                           " QMenu::item { color: #fff; padding: 8px 16px; border-radius: 6px; }"
                           " QMenu::item:selected { background: rgba(255,255,255,0.16); }")
        acts = {menu.addAction(tr(self.lang, "clip_mode_share")): "share",
                menu.addAction(tr(self.lang, "clip_mode_hd")): "hd",
                menu.addAction(tr(self.lang, "clip_mode_original")): "original"}
        chosen = menu.exec(self.btn_save_clip.mapToGlobal(self.btn_save_clip.rect().topLeft()))
        if chosen is None:
            return
        main = self.parent().window() if self.parent() is not None else None
        if main is None or not hasattr(main, "make_clip"):
            return
        self.player.pause()
        self.btn_play.setIcon(QIcon(emoji_icon("▶", 18)))
        if main.make_clip(self._path, rng[0] / 1000.0, rng[1] / 1000.0, acts[chosen], parent=self):
            show_toast(self, tr(self.lang, "clip_started"))

    def _on_track(self, token, track):
        if token != self._load_gen:
            return                       # llego tarde: es del video anterior
        if track and track.get("points"):
            self._track = track
            self.btn_map.setEnabled(True)
            self.btn_map.setToolTip(tr(self.lang, "map_toggle"))
        else:
            self.btn_map.setToolTip(tr(self.lang, "gps_nofix" if track and track.get("nofix") else "gps_none"))

    def toggle_map(self):
        SoundManager.play_click()
        if self.map_panel is None and self._track:
            from ..ui.map import TileMapWidget
            accent = getattr(self.window().parent(), "get_current_theme_color", lambda: "#00f2fe")() \
                if self.parent() is not None else "#00f2fe"
            self.map_panel = TileMapWidget(self._track["points"], accent, self, elapsed=self._track.get("elapsed"),
                                           lang=getattr(self, "lang", "es"))
            self.map_panel.fracs = self._track.get("fracs")     # mismo momento que el grafico
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
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._update_nav_buttons()
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
        self._seek_target = value
        self._update_time_label(value, self.player.duration())
        if not self._seek_timer.isActive():
            self._seek_timer.start()

    def _do_seek(self, value):
        self._seek_timer.stop()
        if value is None:
            return
        self._seek_target = None
        self.player.setPosition(int(value))
        if self.map_panel is not None and self.map_panel.isVisible() and self.player.duration():
            self.map_panel.set_progress(value / self.player.duration())

    def _seek_by(self, ms):
        dur = self.player.duration()
        if dur:
            value = max(0, min(dur, self.player.position() + ms))
            self.slider_seek.setValue(value)
            self._do_seek(value)

    def on_volume(self, value):
        self.audio.setVolume(value / 100.0)
        # Subir el volumen con el control saca el silencio (como en YouTube)
        if value > 0 and self.audio.isMuted():
            self.set_muted(False)
        else:
            self._update_mute_icon()

    def set_muted(self, muted):
        self.audio.setMuted(bool(muted))
        self._update_mute_icon()

    def toggle_mute(self):
        SoundManager.play_click()
        self.set_muted(not self.audio.isMuted())

    def _update_mute_icon(self):
        silent = self.audio.isMuted() or self.slider_volume.value() == 0
        self.btn_mute.setChecked(self.audio.isMuted())
        self.btn_mute.setIcon(QIcon(emoji_icon("🔇" if silent else "🔊", 18)))
        self.btn_mute.setToolTip(tr(self.lang, "player_unmute" if self.audio.isMuted() else "player_mute"))

    def _change_volume(self, delta):
        self.slider_volume.setValue(max(0, min(100, self.slider_volume.value() + delta)))

    def on_position_changed(self, pos):
        if self._clip_stop_at is not None and pos >= self._clip_stop_at:
            self._clip_stop_at = None           # fin de la vista previa del fragmento
            self.player.pause()
            self.btn_play.setIcon(QIcon(emoji_icon("▶", 18)))
        if not self.slider_seek.isSliderDown() and self._seek_target is None:
            self.slider_seek.setValue(pos)
        if self.map_panel is not None and self.map_panel.isVisible() and self.player.duration():
            self.map_panel.set_progress(pos / self.player.duration())
        self._update_time_label(pos, self.player.duration())

    def on_duration_changed(self, dur):
        self.slider_seek.setRange(0, dur)
        self._update_time_label(self.player.position(), dur)
        self._update_clip_ui()

    def _update_time_label(self, pos, dur):
        def fmt(ms):
            s = max(0, ms) // 1000
            return f"{s // 60:02d}:{s % 60:02d}"
        self.lbl_time.setText(f"{fmt(pos)} / {fmt(dur)}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.toggle_play()
        elif event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.step_media(1 if event.key() == Qt.Key.Key_PageDown else -1)   # Re Pag / Av Pag: otro video
        elif event.key() == Qt.Key.Key_I:
            self.mark_in()
        elif event.key() == Qt.Key.Key_O:
            self.mark_out()
        elif event.key() == Qt.Key.Key_S:
            self.toggle_mute()
        elif event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._change_volume(5 if event.key() == Qt.Key.Key_Up else -5)   # flechas arriba/abajo: volumen
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.step_media(1 if event.key() == Qt.Key.Key_Right else -1)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._seek_by(5000 if event.key() == Qt.Key.Key_Right else -5000)   # flechas: 5 s
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


class SeekSlider(QSlider):
    """Barra de avance: un clic en cualquier punto salta ahi (QSlider de Qt
    solo avanzaba 10 ms por clic, parecia que no hacia nada) y se puede
    seguir arrastrando desde ese punto. Marca el fragmento elegido para
    recortar (mark_in / mark_out, en ms)."""
    mark_in = None
    mark_out = None
    hilights = ()          # momentos HiLight de la camara (ms): rayita amarilla sobre la barra
    user_marks = ()        # momentos marcados a mano (ms): rayita blanca sobre la barra
    jump_marks = ()        # saltos detectados (ms): punto naranja abajo

    def _paint_moments(self):
        if self.maximum() <= self.minimum() or not (self.hilights or self.user_marks or self.jump_marks):
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        cy = self.height() / 2
        for values, color, top in ((self.jump_marks, "#ff9f1c", False), (self.user_marks, "#ffffff", True),
                                   (self.hilights, "#ffd166", True)):
            p.setBrush(QColor(color))
            for v in values:
                if not self.minimum() <= v <= self.maximum():
                    continue
                x = groove.x() + handle.width() / 2 + QStyle.sliderPositionFromValue(
                    self.minimum(), self.maximum(), int(v), span)
                if top:
                    p.setPen(QPen(QColor(0, 0, 0, 160), 1))
                    p.drawRoundedRect(QRectF(x - 2.5, cy - 8, 5, 16), 2, 2)
                    p.setPen(Qt.PenStyle.NoPen)
                else:
                    p.drawEllipse(QPointF(x, cy + 8), 2.5, 2.5)
        p.end()

    def paintEvent(self, event):
        super().paintEvent(event)
        self._paint_moments()
        if self.mark_in is None or self.maximum() <= self.minimum():
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())

        def x_of(v):
            return groove.x() + handle.width() / 2 + QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), int(v), span)
        a = x_of(self.mark_in)
        b = x_of(self.mark_out) if self.mark_out is not None else a
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 209, 102, 150))
        p.drawRoundedRect(QRectF(a, cy - 4, max(2.0, b - a), 8), 3, 3)
        p.setBrush(QColor("#ffd166"))
        for x in ((a, b) if self.mark_out is not None else (a,)):
            p.drawRect(QRectF(x - 1.5, cy - 9, 3, 18))
        p.end()
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                 QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(event.position().toPoint()):
                groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                     QStyle.SubControl.SC_SliderGroove, self)
                x = int(event.position().x()) - groove.x() - handle.width() // 2
                span = max(1, groove.width() - handle.width())
                value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span)
                self.setValue(value)
                self.sliderMoved.emit(value)
        super().mousePressEvent(event)


class ZoomableGraphicsView(QGraphicsView):
    """QGraphicsView mit Mausrad-Zoom (Verschieben durch Ziehen ist ueber
    ScrollHandDrag bereits eingebaut)."""
    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


class ImageViewerDialog(QDialog):
    """Visor de fotos con zoom (rueda del mouse) y desplazamiento (arrastrar).
    Doble clic vuelve a "ajustar a la ventana". Con 'playlist' se pasa a la
    foto anterior / siguiente con las flechas o los botones de los costados
    (la siguiente se va cargando en segundo plano)."""
    def __init__(self, path, parent=None, playlist=None, lang="es"):
        super().__init__(parent)
        self.lang = lang
        self.playlist = [x for x in (playlist or []) if x]
        if path not in self.playlist:
            self.playlist = [path]
        self._path = path
        self._cache = {}            # ruta -> QImage ya decodificada (la actual y las vecinas)
        self._loading = set()
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

        self.scene = QGraphicsScene(self)
        self.pixmap_item = self.scene.addPixmap(QPixmap())
        self.view.setScene(self.scene)
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

        nav_style = ("QPushButton { background: rgba(0,0,0,0.45); border-radius: 24px; border: none;"
                     " color: #fff; font-size: 26px; font-weight: 700; padding-bottom: 3px; }"
                     " QPushButton:hover { background: rgba(255,255,255,0.22); }"
                     " QPushButton:disabled { background: rgba(0,0,0,0.15); color: rgba(255,255,255,0.2); }")
        self.btn_prev = QPushButton("‹", self)
        self.btn_next = QPushButton("›", self)
        for b, d, key in ((self.btn_prev, -1, "nav_prev_photo"), (self.btn_next, 1, "nav_next_photo")):
            b.setFixedSize(48, 48)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(nav_style)
            b.setToolTip(tr(lang, key))
            b.clicked.connect(lambda _c=False, d=d: self.step(d))
            b.setVisible(len(self.playlist) > 1)
        self.lbl_pos = QLabel(self)
        self.lbl_pos.setStyleSheet("background: rgba(0,0,0,0.55); color: rgba(255,255,255,0.85); border-radius: 10px;"
                                   " padding: 4px 10px; font-size: 12px;")
        self.show_path(path)

    def _read(self, path):
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        return reader.read()

    def _prefetch(self):
        try:
            i = self.playlist.index(self._path)
        except ValueError:
            return
        want = [self.playlist[j] for j in (i + 1, i - 1) if 0 <= j < len(self.playlist)]
        keep = set(want) | {self._path}
        for k in list(self._cache):
            if k not in keep:
                del self._cache[k]
        for p in want:
            if p in self._cache or p in self._loading:
                continue
            self._loading.add(p)
            th = ScanThread(0, self._read, p)
            th.done.connect(lambda _t, img, p=p: self._on_prefetched(p, img))
            th.start()

    def _on_prefetched(self, path, img):
        self._loading.discard(path)
        if img is not None and not img.isNull():
            self._cache[path] = img

    def show_path(self, path):
        self._path = path
        self.setWindowTitle(os.path.basename(path))
        img = self._cache.get(path)
        if img is None:
            img = self._read(path)
            if not img.isNull():
                self._cache[path] = img
        pix = QPixmap.fromImage(img) if img is not None and not img.isNull() else QPixmap()
        self.pixmap_item.setPixmap(pix)
        self.scene.setSceneRect(QRectF(pix.rect()))
        try:
            i = self.playlist.index(path)
        except ValueError:
            i = 0
        n = len(self.playlist)
        self.btn_prev.setEnabled(i > 0)
        self.btn_next.setEnabled(i < n - 1)
        self.lbl_pos.setText(f"{i + 1} / {n}  ·  {os.path.basename(path)}")
        self.lbl_pos.adjustSize()
        self.lbl_pos.setVisible(n > 1)
        QTimer.singleShot(0, self._fit_to_view)
        QTimer.singleShot(0, self._prefetch)

    def step(self, delta):
        try:
            i = self.playlist.index(self._path)
        except ValueError:
            return
        j = i + delta
        if 0 <= j < len(self.playlist):
            self.show_path(self.playlist[j])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.btn_full.move(self.width() - 50, 12)
        self.btn_full.raise_()
        y = (self.height() - 48) // 2
        self.btn_prev.move(14, y)
        self.btn_next.move(self.width() - 62, y)
        self.lbl_pos.move(12, 12)
        for w in (self.btn_prev, self.btn_next, self.lbl_pos):
            w.raise_()
        QTimer.singleShot(0, self._fit_to_view)

    def toggle_fullscreen(self):
        toggle_fullscreen(self)
        self.btn_full.setIcon(fullscreen_icon(leave=self.isFullScreen()))

    def keyPressEvent(self, event):
        k = event.key()
        if k in (Qt.Key.Key_F11, Qt.Key.Key_F):
            self.toggle_fullscreen()
        elif k == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
            self.step(1)
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_PageUp, Qt.Key.Key_Backspace):
            self.step(-1)
        elif k == Qt.Key.Key_Home and self.playlist:
            self.show_path(self.playlist[0])
        elif k == Qt.Key.Key_End and self.playlist:
            self.show_path(self.playlist[-1])
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
