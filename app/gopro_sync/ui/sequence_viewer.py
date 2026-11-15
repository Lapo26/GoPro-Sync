"""Visor de secuencias de fotos: reproduce un time-lapse como un video y
muestra una rafaga foto por foto para quedarse con la mejor."""
import os
import threading
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QRectF, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QWidget)
from ..env import FONT_STACK
from ..i18n import tr
from ..sound import SoundManager
from ..winshell import move_to_recycle_bin
from ..ui.graphics import emoji_icon
from ..ui.widgets import show_toast
from ..ui.viewers import ImageViewerDialog, SeekSlider, fullscreen_icon, toggle_fullscreen


class FrameLoader(QThread):
    """Decodifica las fotos a tamano de pantalla, en paralelo, y las guarda
    como JPEG en memoria: 600 fotos de 12 MP quedan en ~150 MB en lugar de
    varios GB, y mostrar una es decodificar un JPEG chico (unos ms).
    Empieza por la foto 'start' y sigue hacia adelante, que es lo que se va a
    reproducir primero."""
    frame_ready = Signal(int)

    def __init__(self, frames, max_side, start=0, parent=None):
        super().__init__(parent)
        self.frames = frames
        self.max_side = max_side
        self.start_at = start
        self.data = [None] * len(frames)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _decode(self, i):
        if self._stop.is_set():
            return i, None
        try:
            reader = QImageReader(self.frames[i])
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and max(size.width(), size.height()) > self.max_side:
                f = self.max_side / max(size.width(), size.height())
                # 1/2, 1/4 u 1/8 exacto: el JPEG se decodifica ya reducido (mucho mas rapido)
                for snap in (0.5, 0.25, 0.125):
                    if f <= snap * 1.15 and f >= snap * 0.7:
                        f = snap
                        break
                reader.setScaledSize(QSize(max(1, int(size.width() * f)), max(1, int(size.height() * f))))
            img = reader.read()
            if img.isNull():
                return i, b""
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "JPG", 88)
            buf.close()
            return i, bytes(ba)
        except Exception:
            return i, b""

    def run(self):
        from concurrent.futures import ThreadPoolExecutor
        n = len(self.frames)
        order = list(range(self.start_at, n)) + list(range(0, self.start_at))
        with ThreadPoolExecutor(max_workers=max(2, min(6, (os.cpu_count() or 4) - 1))) as pool:
            for k in range(0, n, 24):
                if self._stop.is_set():
                    return
                for i, data in pool.map(self._decode, order[k:k + 24]):
                    if self._stop.is_set():
                        return
                    if data is not None:
                        self.data[i] = data
                        self.frame_ready.emit(i)


class _Bridge(QObject):
    ready = Signal(int, int, QImage)       # generacion, cuadro, imagen


def _decode_jpeg(data, target):
    """Decodifica un cuadro guardado en memoria al tamano de pantalla. El
    JPEG se reduce al decodificar (1/2, 1/4...) cuando sobra resolucion."""
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf, b"JPG")
    size = reader.size()
    if size.isValid():
        fit = size.scaled(target, Qt.AspectRatioMode.KeepAspectRatio)
        f = fit.width() / max(1, size.width())
        for snap in (0.5, 0.25, 0.125):
            if f <= snap:
                reader.setScaledSize(QSize(max(1, int(size.width() * snap)), max(1, int(size.height() * snap))))
    img = reader.read()
    buf.close()
    if not img.isNull() and img.format() != QImage.Format.Format_RGB32:
        img = img.convertToFormat(QImage.Format.Format_RGB32)   # formato que se dibuja sin convertir
    return img


class FrameView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.img = None
        self.message = ""
        self.corner = ""
        self.setMinimumSize(320, 240)

    def set_image(self, img, message=""):
        self.img = img
        self.message = message
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000"))
        if self.img is not None and not self.img.isNull():
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            s = self.img.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            r = QRectF((self.width() - s.width()) / 2, (self.height() - s.height()) / 2, s.width(), s.height())
            p.drawImage(r, self.img)
        if self.message:
            p.setPen(QColor(255, 255, 255, 190))
            f = p.font()
            f.setPixelSize(15)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
        if self.corner:
            f = p.font()
            f.setPixelSize(12)
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(self.corner) + 20
            r = QRectF(12, 12, w, 24)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(r, 10, 10)
            p.setPen(QColor(255, 255, 255, 210))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.corner)
        p.end()


class SequenceViewerDialog(QDialog):
    FPS_CHOICES = (5, 10, 15, 24, 30, 60)

    def __init__(self, seq, lang, parent=None, start=0, accent="#00f2fe"):
        super().__init__(parent)
        self.seq, self.lang, self.accent = seq, lang, accent
        self.frames = list(seq["frames"])
        self.burst = seq.get("kind") == "burst"
        self.changed = False          # se borraron fotos: la galeria tiene que recargar
        self.index = max(0, min(start, len(self.frames) - 1))
        self._decoded = {}            # pocas QImage ya decodificadas (la actual y las que siguen)
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=3)
        self._inflight = set()
        self._dec_gen = 0
        self._bridge = _Bridge()
        self._bridge.ready.connect(self._on_decoded)
        self.setWindowTitle(tr(lang, "seq_title_burst" if self.burst else "seq_title_timelapse",
                               g=seq["group"], n=len(self.frames)))
        self.resize(1100, 760)
        self.setStyleSheet(
            f"QDialog {{ background: #000; }} * {{ color: #fff; font-family: {FONT_STACK}; }}"
            " QLabel { background: transparent; }"
            " QPushButton#txt { background: rgba(255,255,255,0.1); border: none; border-radius: 14px; padding: 7px 14px;"
            " font-size: 12px; font-weight: 600; } QPushButton#txt:hover { background: rgba(255,255,255,0.2); }"
            f" QPushButton#primary {{ background: {accent}; color: #0d0d12; border: none; border-radius: 14px;"
            " padding: 7px 16px; font-size: 12px; font-weight: 800; }"
            " QComboBox { background: rgba(255,255,255,0.1); border: none; border-radius: 10px; padding: 5px 10px; }"
            " QComboBox QAbstractItemView { background: #23232b; selection-background-color: rgba(255,255,255,0.16); }"
            " QListWidget { background: #0f0f15; border: none; }"
            " QScrollBar:horizontal { background: #0f0f15; height: 10px; margin: 0; }"
            " QScrollBar::handle:horizontal { background: rgba(255,255,255,0.22); border-radius: 5px; min-width: 40px; }"
            " QScrollBar::handle:horizontal:hover { background: rgba(255,255,255,0.35); }"
            " QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
            " QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }"
            " QListWidget::item { border: 2px solid transparent; border-radius: 6px; }"
            f" QListWidget::item:selected {{ border: 2px solid {accent}; background: transparent; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.view = FrameView()
        self.view.mouseDoubleClickEvent = lambda e: self.toggle_fullscreen()
        lay.addWidget(self.view, 1)

        # Tira de miniaturas (rafagas): elegir la mejor de un vistazo
        self.strip = None
        if self.burst:
            self.strip = QListWidget()
            self.strip.setViewMode(QListWidget.ViewMode.IconMode)
            self.strip.setFlow(QListWidget.Flow.LeftToRight)
            self.strip.setWrapping(False)
            self.strip.setIconSize(QSize(128, 96))
            self.strip.setFixedHeight(122)
            self.strip.setSpacing(4)
            self.strip.setMovement(QListWidget.Movement.Static)
            self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.strip.setFixedHeight(136)
            for i in range(len(self.frames)):
                it = QListWidgetItem()
                it.setSizeHint(QSize(136, 104))
                it.setToolTip(os.path.basename(self.frames[i]))
                self.strip.addItem(it)
            self.strip.currentRowChanged.connect(lambda r: self.show_frame(r) if r >= 0 and r != self.index else None)
            lay.addWidget(self.strip)

        controls = QWidget()
        controls.setObjectName("bar")
        controls.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        controls.setStyleSheet("QWidget#bar { background: #15151c; }")
        c = QHBoxLayout(controls)
        c.setContentsMargins(16, 10, 16, 10)
        c.setSpacing(10)

        def round_btn(ch, tip):
            b = QPushButton()
            b.setIcon(QIcon(emoji_icon(ch, 18)))
            b.setIconSize(QSize(18, 18))
            b.setFixedSize(38, 38)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet("QPushButton { background: rgba(255,255,255,0.1); border-radius: 19px; border: none; }"
                            " QPushButton:hover { background: rgba(255,255,255,0.2); }"
                            " QPushButton:checked { background: rgba(0,242,254,0.35); }")
            return b

        self.btn_prev = round_btn("⏮", tr(lang, "seq_prev_frame"))
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_play = round_btn("▶", tr(lang, "seq_play"))
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_next = round_btn("⏭", tr(lang, "seq_next_frame"))
        self.btn_next.clicked.connect(lambda: self.step(1))
        for b in (self.btn_prev, self.btn_play, self.btn_next):
            c.addWidget(b)
        self.lbl_pos = QLabel("")
        self.lbl_pos.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.75);")
        self.lbl_pos.setMinimumWidth(90)
        c.addWidget(self.lbl_pos)
        self.slider = SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(0, len(self.frames) - 1))
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.setMinimumWidth(180)
        self.slider.valueChanged.connect(lambda v: self.show_frame(v) if v != self.index else None)
        c.addWidget(self.slider, 1)
        self.fps = QComboBox()
        for f in self.FPS_CHOICES:
            self.fps.addItem(tr(lang, "seq_fps", n=f), f)
        self.fps.setCurrentIndex(self.FPS_CHOICES.index(10 if self.burst else 24))
        self.fps.setToolTip(tr(lang, "seq_speed"))
        self.fps.currentIndexChanged.connect(self._fps_changed)
        c.addWidget(self.fps)
        self.btn_loop = round_btn("🔁", tr(lang, "seq_loop"))
        self.btn_loop.setCheckable(True)
        self.btn_loop.setChecked(True)
        c.addWidget(self.btn_loop)
        self.btn_photo = QPushButton(tr(lang, "seq_open_photo"))
        self.btn_photo.setObjectName("txt")
        self.btn_photo.clicked.connect(self.open_photo)
        c.addWidget(self.btn_photo)
        if self.burst:
            self.btn_keep = QPushButton(tr(lang, "seq_keep_this"))
            self.btn_keep.setObjectName("txt")
            self.btn_keep.clicked.connect(self.keep_current)
            c.addWidget(self.btn_keep)
        self.btn_export = QPushButton(tr(lang, "seq_export_burst" if self.burst else "seq_export"))
        self.btn_export.setObjectName("primary")
        self.btn_export.clicked.connect(self.export)
        c.addWidget(self.btn_export)
        self.btn_full = round_btn("📷", tr(lang, "fullscreen_toggle"))
        self.btn_full.setIcon(fullscreen_icon())
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        c.addWidget(self.btn_full)
        lay.addWidget(controls)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self._fps_changed()

        screen = self.screen()
        side = 1600
        if screen is not None:
            g = screen.geometry()
            side = int(max(g.width(), g.height()) * screen.devicePixelRatio())
        self.loader = FrameLoader(self.frames, max(960, min(side, 2000)), self.index, self)
        self.loader.frame_ready.connect(self._on_frame_ready)
        self._loaded = 0
        self.loader.start()
        self.show_frame(self.index)

    # ---- cuadros
    # Cada cuadro se decodifica en hilos aparte, unos cuadros por delante de
    # lo que se esta viendo: en el hilo de la interfaz solo se dibuja.
    def _target(self):
        dpr = self.devicePixelRatioF()
        return QSize(max(1, int(self.view.width() * dpr)), max(1, int(self.view.height() * dpr)))

    def _image(self, i):
        img = self._decoded.get(i)
        if img is not None:
            return img
        data = self.loader.data[i] if 0 <= i < len(self.loader.data) else None
        if not data:
            return None
        img = _decode_jpeg(data, self._target())        # no estaba adelantado (salto con la barra)
        self._decoded[i] = img
        return img

    def _window(self):
        n = len(self.frames)
        ahead = [(self.index + k) % n if self.btn_loop.isChecked() else self.index + k for k in range(1, 13)]
        return [j for j in ahead if 0 <= j < n]

    def _prefetch(self):
        want = self._window()
        keep = set(want) | {self.index, max(0, self.index - 1)}
        for k in list(self._decoded):
            if k not in keep:
                del self._decoded[k]
        target, gen = self._target(), self._dec_gen
        for j in want:
            if j in self._decoded or j in self._inflight or not self.loader.data[j]:
                continue
            self._inflight.add(j)
            self._pool.submit(self._decode_job, gen, j, self.loader.data[j], target)

    def _decode_job(self, gen, j, data, target):
        try:
            img = _decode_jpeg(data, target)
        except Exception:
            img = QImage()
        self._bridge.ready.emit(gen, j, img)

    def _on_decoded(self, gen, j, img):
        self._inflight.discard(j)
        if gen == self._dec_gen and not img.isNull():
            self._decoded[j] = img

    def _on_frame_ready(self, i):
        self._loaded += 1
        if self.strip is not None:
            img = QImage.fromData(self.loader.data[i], "JPG") if self.loader.data[i] else None
            if img is not None and not img.isNull():
                small = img.scaled(128, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.strip.item(i).setIcon(QIcon(QPixmap.fromImage(small)))
        if i == self.index:
            self.show_frame(i)
        else:
            self._update_label()

    def show_frame(self, i):
        if not self.frames:
            return
        i = max(0, min(i, len(self.frames) - 1))
        self.index = i
        img = self._image(i)
        self.view.set_image(img if img is not None else self.view.img,
                            "" if img is not None else tr(self.lang, "seq_loading_frame"))
        self._prefetch()
        if self.slider.value() != i:
            self.slider.blockSignals(True)
            self.slider.setValue(i)
            self.slider.blockSignals(False)
        if self.strip is not None and self.strip.currentRow() != i:
            self.strip.blockSignals(True)
            self.strip.setCurrentRow(i)
            self.strip.blockSignals(False)
        self._update_label()

    def _update_label(self):
        n = len(self.frames)
        self.lbl_pos.setText(f"{self.index + 1} / {n}")
        corner = tr(self.lang, "seq_loading", k=self._loaded, n=n) if self._loaded < n else ""
        if corner != self.view.corner:
            self.view.corner = corner
            self.view.update()

    def step(self, d):
        self.pause()
        self.show_frame(self.index + d)

    # ---- reproduccion
    def _fps_changed(self, *_):
        self.timer.setInterval(max(10, int(1000 / (self.fps.currentData() or 24))))

    def toggle_play(self):
        SoundManager.play_click()
        if self.timer.isActive():
            self.pause()
        else:
            if self.index >= len(self.frames) - 1:
                self.show_frame(0)
            self.timer.start()
            self.btn_play.setIcon(QIcon(emoji_icon("⏸", 18)))

    def pause(self):
        self.timer.stop()
        self.btn_play.setIcon(QIcon(emoji_icon("▶", 18)))

    def _tick(self):
        nxt = self.index + 1
        if nxt >= len(self.frames):
            if not self.btn_loop.isChecked():
                self.pause()
                return
            nxt = 0
        if self.loader.data[nxt] is None:
            return                     # todavia cargando: esperar sin saltar fotos
        if nxt not in self._decoded and nxt in self._inflight:
            return                     # se esta decodificando: esperar un instante en vez de trabar la ventana
        self.show_frame(nxt)

    # ---- acciones
    def open_photo(self):
        self.pause()
        ImageViewerDialog(self.frames[self.index], self, playlist=self.frames, lang=self.lang).exec()

    def export(self):
        self.pause()
        gallery = self.parent()
        if gallery is not None and hasattr(gallery, "export_sequence"):
            gallery.export_sequence(self.seq)

    def keep_current(self):
        """Rafaga: quedarse con la foto que se ve y mandar las otras a la Papelera."""
        from ..ui.dialogs import ConfirmDeleteDialog
        self.pause()
        keep = self.frames[self.index]
        others = [p for p in self.frames if p != keep]
        if not others:
            return
        dlg = ConfirmDeleteDialog(self, self.lang, keep,
                                  question=tr(self.lang, "seq_keep_question", n=len(others), name=os.path.basename(keep)))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        raws = [os.path.splitext(p)[0] + ".GPR" for p in others]
        raws = [r for r in raws if os.path.exists(r)]
        if move_to_recycle_bin(others + raws):
            self.changed = True
            show_toast(self.parent() or self, tr(self.lang, "seq_kept", name=os.path.basename(keep)))
            self.accept()
        else:
            show_toast(self, tr(self.lang, "delete_failed", name=os.path.basename(others[0])))

    def toggle_fullscreen(self):
        toggle_fullscreen(self)
        self.btn_full.setIcon(fullscreen_icon(leave=self.isFullScreen()))

    def keyPressEvent(self, event):
        k = event.key()
        if k == Qt.Key.Key_Space:
            self.toggle_play()
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self.step(10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.step(-10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else -1)
        elif k == Qt.Key.Key_Home:
            self.step(-len(self.frames))
        elif k == Qt.Key.Key_End:
            self.step(len(self.frames))
        elif k in (Qt.Key.Key_F11, Qt.Key.Key_F):
            self.toggle_fullscreen()
        elif k == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._decoded.clear()           # se decodifican de nuevo al tamano nuevo (nitidas en pantalla completa)
        self._dec_gen += 1
        if hasattr(self, "loader"):
            QTimer.singleShot(0, lambda: self.show_frame(self.index))

    def done(self, r):
        self.timer.stop()
        self._dec_gen += 1
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.loader.stop()
        self.loader.wait(3000)
        super().done(r)
