"""Trabajo en segundo plano mientras la ventana esta minimizada o cerrada
junto al reloj: aprovecha ese tiempo para dejar la biblioteca lista.

- GPS de la biblioteca (mapa global y mapa del reproductor): lo leen los
  procesos del indice (gps_index), con prioridad baja. Siguen aunque la
  ventana vuelva a abrirse (no compiten con ella).
- Miniaturas de la galeria que faltan: se generan de a una en un hilo, lo
  mas nuevo primero. Solo mientras la ventana no esta a la vista: generar
  usa Qt (decodificar, achicar) y en PySide eso retiene el GIL, lo que
  trabaria la ventana. Al volver a abrirla se pausa y sigue despues.

El progreso se muestra junto al reloj (texto al pasar el mouse, una linea en
el menu y un aro en el icono) y en el boton de la barra de tareas si la
ventana esta minimizada. Sin globos de aviso."""
import os
import threading
import time
from PySide6.QtCore import QObject, QTimer, Signal
from ..gps_index import default_db_path, progress_of, read_meta
from ..i18n import tr

_VIDEO_EXTS = (".mp4",)
_PHOTO_EXTS = (".jpg", ".jpeg", ".png")
_SKIP_DIRS = {"$recycle.bin", "system volume information", "thumb_cache", "__pycache__"}


class _ThumbPregen(threading.Thread):
    """Genera las miniaturas que faltan (las mismas que usa la galeria, en
    su cache). Se puede pausar y cortar en cualquier momento."""

    def __init__(self, folder, lang):
        super().__init__(daemon=True, name="miniaturas-fondo")
        self.folder, self.lang = folder, lang
        self.state = "scanning"
        self.done = self.total = 0
        self.run_gate = threading.Event()       # puesto = puede trabajar
        self.run_gate.set()
        self.stop_flag = False

    def _walk(self):
        out, stack = [], [self.folder]
        while stack and not self.stop_flag:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if not e.name.startswith(".") and e.name.lower() not in _SKIP_DIRS:
                                    stack.append(e.path)
                                continue
                            low = e.name.lower()
                            if low.startswith("._"):
                                continue
                            kind = "video" if low.endswith(_VIDEO_EXTS) else "picture" if low.endswith(_PHOTO_EXTS) else None
                            if kind:
                                st = e.stat()
                                out.append((e.path, kind, st.st_size, st.st_mtime))
                        except OSError:
                            continue
            except OSError:
                continue
        return out

    def run(self):
        from .thumbs import _ThumbIO, _gen_lock
        try:
            files = self._walk()
            ios = {k: _ThumbIO(k, {}, self.lang) for k in ("video", "picture")}
            todo = []
            for path, kind, size, mtime in files:
                io = ios[kind]
                io.meta[path] = (size, mtime)
                cache = io._cache_path(path)
                if cache and not os.path.exists(cache):
                    todo.append((mtime, path, kind))
            todo.sort(reverse=True)                     # lo mas nuevo primero
            self.total = len(todo)
            self.state = "working"
            for _mtime, path, kind in todo:
                self.run_gate.wait()
                if self.stop_flag:
                    return
                io = ios[kind]
                try:
                    cache = io._cache_path(path)
                    if cache and not os.path.exists(cache):     # la galeria pudo haberla hecho
                        with _gen_lock(kind):
                            io._load_raw(path)
                except Exception as e:
                    print("[Miniaturas de fondo]", path, e)
                self.done += 1
        finally:
            self.state = "done"


class BackgroundWork(QObject):
    """Arranca y sigue el trabajo de fondo; avisa el progreso con
    status_changed(texto o None, porcentaje o None)."""
    status_changed = Signal(object, object)
    all_done = Signal()
    TICK_MS = 1500
    COOLDOWN_S = 600        # si termino hace poco con la misma carpeta, no volver a revisar todo

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.db = default_db_path()
        self._thumbs = None
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.text = None
        self._last_done = None          # (momento, carpeta) de la ultima vez que termino todo
        self._run_folder = None

    @property
    def lang(self):
        return getattr(self.window, "lang", "es")

    def _folder(self):
        sp = getattr(self.window, "sync_page", None)
        folder = getattr(sp, "sync_target_dir", None)
        return folder if folder and os.path.isdir(folder) else None

    def busy(self):
        return self._active

    def start(self, force=False):
        """Ventana minimizada o cerrada junto al reloj: poner manos a la obra.
        False si no hay nada que hacer (sin carpeta, o se reviso hace poco)."""
        folder = self._folder()
        if folder is None:
            return False
        if (not force and not self._active and self._last_done is not None
                and self._last_done[1] == folder and time.monotonic() - self._last_done[0] < self.COOLDOWN_S):
            return False
        self._active = True
        self._run_folder = folder
        page = getattr(self.window, "map_page", None)
        if page is not None:
            page.start_indexing()
        t = self._thumbs
        if t is None or not t.is_alive() or t.folder != folder:
            if t is not None:
                t.stop_flag = True
                t.run_gate.set()
            self._thumbs = _ThumbPregen(folder, self.lang)
            self._thumbs.start()
        self.set_window_visible(self._window_visible())
        self._timer.start()
        self._tick()
        return True

    def _window_visible(self):
        w = self.window
        return w.isVisible() and not w.isMinimized()

    def set_window_visible(self, visible):
        """Las miniaturas solo con la ventana fuera de la vista (ver arriba)."""
        t = self._thumbs
        if t is not None:
            if visible:
                t.run_gate.clear()
            else:
                t.run_gate.set()

    def stop(self, wait_s=2.0):
        """Al salir de la app: cortar y esperar un momento a que el hilo deje
        lo que estaba haciendo (salir con Qt a mitad de una imagen puede caerse)."""
        self._timer.stop()
        t = self._thumbs
        if t is not None:
            t.stop_flag = True
            t.run_gate.set()
            if wait_s and t.is_alive():
                t.join(wait_s)

    def _tick(self):
        parts, fracs = [], []
        page = getattr(self.window, "map_page", None)
        gps_running = bool(page is not None and page._procs)
        if gps_running:
            state, done, total = progress_of(read_meta(self.db))
            if state == "indexing" and total:
                parts.append(tr(self.lang, "bg_gps", done=done, total=total))
                fracs.append(done / total)
            else:
                parts.append(tr(self.lang, "bg_gps_scan"))
        t = self._thumbs
        thumbs_running = t is not None and t.is_alive()
        if thumbs_running:
            if t.state == "scanning":
                parts.append(tr(self.lang, "bg_thumbs_scan"))
            elif t.total:
                key = "bg_thumbs_paused" if not t.run_gate.is_set() else "bg_thumbs"
                parts.append(tr(self.lang, key, done=t.done, total=t.total))
                fracs.append(t.done / t.total)
        if not gps_running and not thumbs_running:
            self._timer.stop()
            was_active, self._active = self._active, False
            self.text = None
            self.status_changed.emit(None, None)
            if was_active:
                self._last_done = (time.monotonic(), self._run_folder)
                self.all_done.emit()
            return
        self.text = "\n".join(parts)
        pct = sum(fracs) / len(fracs) * 100 if fracs else None
        self.status_changed.emit(self.text, pct)
