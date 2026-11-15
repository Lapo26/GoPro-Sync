"""Galerias de videos y fotos."""
import os
import re
from datetime import datetime
from datetime import timedelta
from PySide6.QtCore import QDate, QTimer, Qt
from PySide6.QtWidgets import QFrame, QApplication, QDateEdit, QDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget
from ..i18n import _human_size, short_path, tr
from ..winshell import move_to_recycle_bin, show_in_explorer
from ..sound import SoundManager
from ..media import gps_flags, is_broken_mp4
from ..env import CONFIG_DIR
from ..workers import ScanThread
from ..videotools import chapter_group_of, find_chapter_groups
from ..sequences import find_sequences, parse_seq_name

# GOPR1234.MP4, GP011234.MP4... y el video ya unido GOPR1234_unido.MP4
_CHAPTER_NAME_RE = re.compile(r"^(GOPR|GP(\d\d))(\d{4})(_unido(?:_\d+)?)?\.MP4$", re.IGNORECASE)

# Un color por video partido, para ver de un vistazo que partes van juntas
_CHAPTER_COLORS = ["#4cc9f0", "#ffd166", "#06d6a0", "#ff6b9a", "#c77dff", "#ff9f1c", "#90be6d", "#f4f1de"]
from ..ui.widgets import show_toast
from ..ui.thumbs import ThumbGridView
from ..ui.viewers import ImageViewerDialog, VideoPlayerDialog
from ..ui.map import GpsMapDialog
from ..ui.dialogs import ConfirmDeleteDialog, MediaInfoDialog

class GalleryBase(QWidget):
    KIND = "picture"       # von Subklassen ueberschrieben: "video" oder "picture"
    EXTENSIONS = ()        # von Subklassen ueberschrieben

    def __init__(self, folder, title, empty_icon, empty_text, lang="de"):
        super().__init__()
        self.folder = folder
        self.lang = lang
        self.col_count = 3
        self.loader_thread = None
        self._loaded_count = 0
        # Sortierreihenfolge: True = neueste zuerst (Standard), False = aelteste zuerst
        self.sort_newest_first = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(18)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(12)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet("font-size: 24px; font-weight: 800; letter-spacing: 0.5px; color: #ffffff; background: transparent;")
        header_row.addWidget(self.title_lbl)
        header_row.addStretch()

        self.btn_sort = QPushButton()
        self.btn_sort.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sort.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.14);
                border-radius: 10px;
                padding: 8px 16px;
                color: rgba(255,255,255,0.85);
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.12);
            }
        """)
        self.btn_sort.clicked.connect(self._show_sort_menu)

        # Filtro por fecha: (desde, hasta) como "AAAA-MM-DD" inclusive, o None
        self.date_filter = None
        self._date_filter_key = "date_all"
        self.btn_date = QPushButton()
        self.btn_date.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_date.setStyleSheet(self.btn_sort.styleSheet())
        self.btn_date.clicked.connect(self._show_date_menu)
        header_row.addWidget(self.btn_date)
        self._update_date_button_text()

        header_row.addWidget(self.btn_sort)
        self._update_sort_button_text()

        outer.addLayout(header_row)
        self.header_row = header_row

        self.lbl_loading = QLabel("")
        self.lbl_loading.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.4); background: transparent;")
        outer.addWidget(self.lbl_loading)
        self.lbl_loading.hide()

        # Vista virtualizada (dibuja solo lo visible); "scroll" se mantiene como
        # nombre por compatibilidad con el resto del codigo.
        self.view = ThumbGridView(self)
        self.view.activated.connect(self._open_item)
        self.view.context.connect(self._show_context_menu)
        self.view.long_pressed.connect(self.enter_selection)
        self.view.selection_changed.connect(self._update_selection_bar)
        self.scroll = self.view
        outer.addWidget(self.view)
        # Barra del modo seleccion (mantener apretado o Ctrl+clic sobre un video)
        self.sel_bar = QFrame()
        self.sel_bar.setStyleSheet(
            "QFrame { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 12px; }"
            " QLabel { border: none; background: transparent; color: #fff; font-size: 13px; font-weight: 700; }"
            " QPushButton { background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);"
            " border-radius: 10px; padding: 8px 16px; font-weight: 600; } QPushButton:hover { background: rgba(255,255,255,0.16); }"
            " QPushButton:disabled { color: rgba(255,255,255,0.35); }")
        bar = QHBoxLayout(self.sel_bar)
        bar.setContentsMargins(16, 8, 10, 8)
        self.lbl_sel = QLabel("")
        self.btn_sel_join = QPushButton(tr(lang, "sel_join"))
        self.btn_sel_join.clicked.connect(self._join_selected)
        self.btn_sel_light = QPushButton(tr(lang, "sel_light"))
        self.btn_sel_light.clicked.connect(self._light_selected)
        self.btn_sel_cancel = QPushButton(tr(lang, "sel_cancel"))
        self.btn_sel_cancel.clicked.connect(self.exit_selection)
        bar.addWidget(self.lbl_sel)
        bar.addStretch()
        bar.addWidget(self.btn_sel_light)
        bar.addWidget(self.btn_sel_join)
        bar.addWidget(self.btn_sel_cancel)
        self.sel_bar.hide()
        outer.addWidget(self.sel_bar)

        self._empty_icon = empty_icon
        self.empty_lbl = QLabel(f"{empty_icon}\n{empty_text}")
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_lbl.setStyleSheet("font-size: 15px; color: rgba(255,255,255,0.4); background: transparent;")
        outer.addWidget(self.empty_lbl)
        self.empty_lbl.hide()

    def _update_sort_button_text(self):
        label = tr(self.lang, "gallery_sort_newest" if self.sort_newest_first else "gallery_sort_oldest")
        self.btn_sort.setText(label)

    def _show_sort_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #23232b;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 10px;
                padding: 6px;
            }
            QMenu::item {
                color: #ffffff;
                padding: 8px 16px;
                border-radius: 6px;
                font-size: 13px;
            }
            QMenu::item:selected {
                background-color: rgba(255,255,255,0.16);
                color: #ffffff;
            }
        """)
        action_newest = menu.addAction(tr(self.lang, "gallery_sort_newest"))
        action_oldest = menu.addAction(tr(self.lang, "gallery_sort_oldest"))
        chosen = menu.exec(self.btn_sort.mapToGlobal(self.btn_sort.rect().bottomLeft()))
        if chosen is None:
            return
        SoundManager.play_click()
        new_value = chosen == action_newest
        if new_value == self.sort_newest_first:
            return
        self.sort_newest_first = new_value
        self._update_sort_button_text()
        self._rescan_next = False
        self.refresh()

    def retranslate(self, title, empty_text):
        self.title_lbl.setText(title)
        self.empty_lbl.setText((f"{self._empty_icon}\n{empty_text}" if self._empty_icon else empty_text))
        self._update_sort_button_text()

    def show_empty(self):
        self.scroll.hide()
        self.empty_lbl.show()

    # ---- modo seleccion (como en la sincronizacion de la camara)
    def enter_selection(self, path=None):
        self.view.selectable = True
        self.view.click_toggles = True
        self.view.selected = {path} if path else set()
        self._sel_had_items = False
        self.btn_sel_join.setVisible(self.KIND == "video")
        self.btn_sel_light.setVisible(self.KIND == "video")
        self.sel_bar.show()
        self.view.viewport().update()
        self._update_selection_bar()

    def exit_selection(self):
        self.view.selectable = False
        self.view.click_toggles = False
        self.view.selected = set()
        self._sel_had_items = False
        self.sel_bar.hide()
        self.view.viewport().update()

    def _update_selection_bar(self):
        if not self.view.selectable:
            return
        sel = [x for x in self.view.selected if x in self.view.files]
        if not sel and self.view.selected is not None and getattr(self, "_sel_had_items", False):
            self.exit_selection()        # se desmarco todo: salir del modo seleccion
            return
        self._sel_had_items = bool(sel) or getattr(self, "_sel_had_items", False)
        total = 0
        for x in sel:
            try:
                total += os.path.getsize(x)
            except OSError:
                pass
        self.lbl_sel.setText(tr(self.lang, "sel_count", n=len(sel), size=_human_size(total)))
        self.btn_sel_join.setEnabled(len(sel) >= 2)
        self.btn_sel_light.setEnabled(len(sel) >= 1)

    def _light_selected(self):
        sel = [x for x in self.view.files if x in self.view.selected]
        main = self.window()
        if hasattr(main, "make_light_versions") and main.make_light_versions(sel):
            self.exit_selection()

    def _join_selected(self):
        sel = [x for x in self.view.files if x in self.view.selected]
        main = self.window()
        if hasattr(main, "join_selected") and main.join_selected(sel):
            self.exit_selection()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.view.selectable:
            self.exit_selection()
        else:
            super().keyPressEvent(event)

    def _forget_missing(self, path):
        """El archivo se borro o movio por fuera de la app: sacarlo de la galeria."""
        if os.path.exists(path):
            return False
        self.view.remove_path(path)
        if getattr(self, "_all_files", None) and path in self._all_files:
            self._all_files.remove(path)
        show_toast(self, tr(self.lang, "file_gone", name=os.path.basename(path)))
        return True

    def _open_item(self, path):
        if self._forget_missing(path):
            return
        if self.KIND == "video":
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size == 0:
                show_toast(self, tr(self.lang, "video_empty_msg", name=os.path.basename(path)))
                return
            if is_broken_mp4(path):
                show_toast(self, tr(self.lang, "video_broken_msg", name=os.path.basename(path)))
                return
            self.play_video(path)
        else:
            self.open_image(path)

    def soft_refresh(self):
        """Revisa la carpeta en segundo plano (al volver a la app): saca lo que
        ya no esta sin perder la posicion, o recarga si hay archivos nuevos."""
        if not self.isVisible() or getattr(self, "_all_files", None) is None:
            return
        if getattr(self, "_soft_running", False):
            self._soft_again = True          # se pidio otra mientras corria: repetir al terminar
            return
        self._soft_running = True
        self._soft_again = False
        folder, exts = self.folder, tuple(e.lower() for e in self.EXTENSIONS)

        def scan():
            found, stack = set(), [folder]
            while stack:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    stack.append(e.path)
                                elif e.name.lower().endswith(exts):
                                    found.add(e.path)
                            except OSError:
                                pass
                except OSError:
                    pass
            return found
        th = ScanThread(0, scan)
        th.done.connect(self._on_soft_scan)
        th.start()

    def _on_soft_scan(self, _t, found):
        self._soft_running = False
        if getattr(self, "_soft_again", False):
            QTimer.singleShot(0, self.soft_refresh)    # el resultado ya puede estar viejo
            return
        if found is None or getattr(self, "_all_files", None) is None:
            return
        current = set(self._all_files)
        if found - current:
            self.refresh()                       # hay archivos nuevos: recargar
            return
        for path in current - found:
            self.view.remove_path(path)
            self._all_files.remove(path)

    def _show_context_menu(self, path, global_pos):
        """Clic derecho en una miniatura: ruta, copiar ruta, Explorador,
        abrir e informacion del archivo (metadatos)."""
        if self._forget_missing(path):
            return
        menu = QMenu(self)
        head = menu.addAction(short_path(path, keep=3))
        head.setEnabled(False)
        menu.addSeparator()
        menu.addAction(tr(self.lang, "ctx_open"),
                       lambda: self.play_video(path) if self.KIND == "video" else self.open_image(path))
        menu.addAction(tr(self.lang, "ctx_info"), lambda: MediaInfoDialog(
            self, self.lang, path, getattr(self.window(), "get_current_theme_color", lambda: "#00f2fe")()).exec())
        menu.addSeparator()
        menu.addAction(tr(self.lang, "ctx_copy_path"), lambda: (QApplication.clipboard().setText(os.path.normpath(path)),
                                                               show_toast(self, tr(self.lang, "path_copied"))))
        menu.addAction(tr(self.lang, "ctx_show_in_explorer"), lambda: show_in_explorer(path))
        main = self.window()
        if self.KIND == "video" and hasattr(main, "make_light_version"):
            menu.addSeparator()
            menu.addAction(tr(self.lang, "ctx_light"), lambda: main.make_light_version(path))
            if not self.view.selectable:
                menu.addAction(tr(self.lang, "ctx_select_many"), lambda: self.enter_selection(path))
            menu.addAction(tr(self.lang, "ctx_map"), lambda: GpsMapDialog(
                self, self.lang, path, getattr(main, "get_current_theme_color", lambda: "#00f2fe")()).exec())
            if os.path.exists(path) and os.path.getsize(path) > 0 and is_broken_mp4(path):
                menu.addAction(tr(self.lang, "ctx_repair"), lambda: main.repair_video(path))
            group = chapter_group_of(path)
            if group and not group["done"] and not group["damaged"] and not group.get("missing"):
                menu.addAction(tr(self.lang, "ctx_join", n=len(group["parts"])), lambda: main.join_groups([group]))
            if group and hasattr(main, "open_join_dialog"):
                menu.addAction(tr(self.lang, "ctx_join_all"), main.open_join_dialog)
        sq = self.sequence_for(path) if hasattr(self, "sequence_for") else None
        if sq is not None:
            n = len(sq["frames"])
            burst = sq["kind"] == "burst"
            menu.addSeparator()
            menu.addAction(tr(self.lang, "ctx_seq_view_burst" if burst else "ctx_seq_play", n=n),
                           lambda: self.open_sequence(sq, sq["frames"].index(path) if path in sq["frames"] else 0))
            menu.addAction(tr(self.lang, "ctx_seq_collapse" if sq["key"] in self._expanded else "ctx_seq_expand", n=n),
                           lambda: self.toggle_sequence(sq))
            menu.addAction(tr(self.lang, "ctx_seq_export_burst" if burst else "ctx_seq_export"),
                           lambda: self.export_sequence(sq))
            menu.addSeparator()
            menu.addAction(tr(self.lang, "ctx_seq_delete", n=n), lambda: self.delete_sequence(sq))
            if sq["key"] in self._expanded:
                menu.addAction(tr(self.lang, "ctx_delete"), lambda: self.delete_file(path))
            menu.exec(global_pos)
            return
        menu.addSeparator()
        menu.addAction(tr(self.lang, "ctx_delete"), lambda: self.delete_file(path))
        menu.exec(global_pos)

    def delete_file(self, path):
        """Eliminar con confirmacion: va a la Papelera de reciclaje."""
        if ConfirmDeleteDialog(self, self.lang, path).exec() != QDialog.DialogCode.Accepted:
            return
        name = os.path.basename(path)
        if move_to_recycle_bin(path):
            self.view.remove_path(path)
            if getattr(self, "_all_files", None) and path in self._all_files:
                self._all_files.remove(path)
            show_toast(self, tr(self.lang, "deleted_ok", name=name))
        else:
            show_toast(self, tr(self.lang, "delete_failed", name=name))

    def _relayout_if_needed(self):
        pass   # la vista virtualizada reacomoda sola al redimensionar

    def showEvent(self, event):
        super().showEvent(event)
        # Las galerias se cargan recien la primera vez que se abren (antes se
        # recorrian las ~3000 fotos/videos al arrancar la app).
        if getattr(self, "_needs_refresh", False):
            self._needs_refresh = False
            QTimer.singleShot(0, self.refresh)

    def _update_date_button_text(self, label=None):
        if label is None:
            label = tr(self.lang, self._date_filter_key) if self._date_filter_key else ""
        self._date_label = label
        self.btn_date.setText(tr(self.lang, "date_filter_btn", label=label))

    def _available_years(self):
        years = set()
        try:
            for name in os.listdir(self.folder):
                if re.match(r"^\d{4}-\d{2}-\d{2}$", name):
                    years.add(name[:4])
        except Exception:
            pass
        return sorted(years, reverse=True)

    def _set_date_filter(self, rng, key=None, label=None):
        self.date_filter = rng
        self._date_filter_key = key
        self._update_date_button_text(label)
        self._rescan_next = False
        self.refresh()

    def _show_date_menu(self):
        today = datetime.now().date()
        fmt = lambda d: d.strftime("%Y-%m-%d")
        menu = QMenu(self)
        menu.addAction(tr(self.lang, "date_all"), lambda: self._set_date_filter(None, "date_all"))
        menu.addAction(tr(self.lang, "date_today"), lambda: self._set_date_filter((fmt(today), fmt(today)), "date_today"))
        menu.addAction(tr(self.lang, "date_7"),
                       lambda: self._set_date_filter((fmt(today - timedelta(days=6)), fmt(today)), "date_7"))
        menu.addAction(tr(self.lang, "date_30"),
                       lambda: self._set_date_filter((fmt(today - timedelta(days=29)), fmt(today)), "date_30"))
        menu.addAction(tr(self.lang, "date_this_year"),
                       lambda: self._set_date_filter((f"{today.year}-01-01", f"{today.year}-12-31"), "date_this_year"))
        years = self._available_years()
        if years:
            menu.addSeparator()
            for y in years:
                menu.addAction(y, lambda y=y: self._set_date_filter((f"{y}-01-01", f"{y}-12-31"), None, y))
        menu.addSeparator()
        menu.addAction(tr(self.lang, "date_custom"), self._pick_date_range)
        menu.exec(self.btn_date.mapToGlobal(self.btn_date.rect().bottomLeft()))

    def _pick_date_range(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(tr(self.lang, "date_range_title"))
        dlg.setStyleSheet("QDialog { background: #1c1e29; } QLabel { color: #fff; }")
        lay = QVBoxLayout(dlg)
        row = QHBoxLayout()
        start = self.date_filter[0] if self.date_filter else datetime.now().strftime("%Y-01-01")
        end = self.date_filter[1] if self.date_filter else datetime.now().strftime("%Y-%m-%d")
        d_from = QDateEdit(QDate.fromString(start, "yyyy-MM-dd"))
        d_to = QDateEdit(QDate.fromString(end, "yyyy-MM-dd"))
        for w in (d_from, d_to):
            w.setCalendarPopup(True)
            w.setDisplayFormat("dd/MM/yyyy")
        row.addWidget(QLabel(tr(self.lang, "date_from")))
        row.addWidget(d_from)
        row.addWidget(QLabel(tr(self.lang, "date_to")))
        row.addWidget(d_to)
        lay.addLayout(row)
        ok = QPushButton(tr(self.lang, "date_apply"))
        ok.clicked.connect(dlg.accept)
        lay.addWidget(ok, alignment=Qt.AlignmentFlag.AlignRight)
        if dlg.exec():
            a, b = d_from.date().toString("yyyy-MM-dd"), d_to.date().toString("yyyy-MM-dd")
            if a > b:
                a, b = b, a
            label = f"{d_from.date().toString('dd/MM/yyyy')} – {d_to.date().toString('dd/MM/yyyy')}" \
                if a <= b else ""
            self._set_date_filter((a, b), None, label)

    def _mtime_for_path(self, path):
        cached = getattr(self, "_mtime_cache", {}).get(path)
        if cached is not None:
            return cached
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0

    def _date_for_path(self, path):
        """Bevorzugt den Datums-Unterordner (YYYY-MM-DD, beim Sync aus dem
        Aufnahmedatum der Datei abgeleitet), faellt sonst auf das
        Datei-Datum (mtime) zurueck - z.B. fuer Dateien, die noch von vor
        der Datums-Sortierung stammen oder direkt im Root liegen."""
        # FECHA\archivo, FECHA\camara\archivo o FECHA\camara\Time Lapse 1\archivo (Quik)
        d = os.path.dirname(path)
        for _ in range(3):
            name = os.path.basename(d)
            if re.match(r"^\d{4}-\d{2}-\d{2}$", name):
                return name
            d = os.path.dirname(d)
        try:
            return datetime.fromtimestamp(self._mtime_for_path(path)).strftime("%Y-%m-%d")
        except Exception:
            return "0000-00-00"

    def get_files(self, exts, rescan=True):
        """Recorre el destino recursivamente y devuelve las rutas ordenadas
        por fecha. Usa os.scandir: en Windows la fecha de cada archivo viene
        con el listado de la carpeta, sin consultar el disco archivo por
        archivo (antes eran miles de consultas: ~4 s en disco mecanico)."""
        if not rescan and getattr(self, "_all_files", None) is not None:
            results = list(self._all_files)          # orden/filtro: sin volver a recorrer el disco
            return self._filter_and_sort(results)
        results = []
        self._mtime_cache = {}
        self._meta = {}            # ruta -> (tamano, fecha) para buscar en la cache sin tocar el disco
        stack = [self.folder]
        try:
            while stack:
                d = stack.pop()
                try:
                    it = os.scandir(d)
                except OSError:
                    continue
                with it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.name.lower().endswith(exts):
                                results.append(e.path)
                                st = e.stat(follow_symlinks=False)
                                self._mtime_cache[e.path] = st.st_mtime
                                self._meta[e.path] = (st.st_size, st.st_mtime)
                        except OSError:
                            continue
        except Exception:
            return []
        # Quik a veces dejo las mismas fotos de rafaga/time-lapse en dos
        # carpetas del mismo dia ("Time Lapse 1" y "Burst Sequence 1"): se
        # muestra cada archivo una sola vez (mismo dia, nombre y tamano). En
        # el disco no se toca nada.
        seen, unique = set(), []
        for p in sorted(results):
            key = (self._date_for_path(p), os.path.basename(p).lower(), self._meta.get(p, (None,))[0])
            if key in seen:
                continue
            seen.add(key)
            unique.append(p)
        results = unique
        self._all_files = list(results)
        return self._filter_and_sort(results)

    def _filter_and_sort(self, results):
        if self.date_filter:
            lo, hi = self.date_filter
            results = [p for p in results if lo <= self._date_for_path(p) <= hi]
        # Zuerst nach dem Datums-Ordner sortieren (derselbe Wert, der auch
        # fuer die "Heute"/"Gestern"-Gruppenkoepfe verwendet wird), erst
        # danach nach mtime als Fein-Sortierung INNERHALB eines Tages.
        # So bleibt jede Tages-Gruppe garantiert am Stueck zusammen, statt
        # sich mit einer anderen Gruppe zu vermischen, wenn die mtime-Werte
        # zweier Tage sich einmal ueberschneiden.
        # Las partes de un video partido (GOPR1234, GP011234, GP021234... y el
        # GOPR1234_unido) van juntas y SIEMPRE en orden 1, 2, 3: se ubican por
        # la fecha de la primera parte. Antes cada parte iba por su propia
        # fecha y con "mas nuevos primero" quedaban al reves (3/3, 2/3, 1/3).
        rev = self.sort_newest_first
        def key(p):
            m = _CHAPTER_NAME_RE.match(os.path.basename(p))
            if not m:
                return (self._date_for_path(p), self._mtime_for_path(p), "", 0)
            num = m.group(3)
            order = -1 if m.group(4) else (0 if m.group(1).upper() == "GOPR" else int(m.group(2)))
            first = os.path.join(os.path.dirname(p), f"GOPR{num}.MP4")
            anchor = self._mtime_cache.get(first) if hasattr(self, "_mtime_cache") else None
            if anchor is None:
                anchor = self._mtime_for_path(p)
            grp = os.path.normcase(first)
            return (self._date_for_path(p), anchor, grp, -order if rev else order)
        results.sort(key=key, reverse=rev)
        return results

    def populate(self):
        """Startet das Laden der Vorschaubilder IM HINTERGRUND (siehe
        ThumbnailLoaderThread) - blockiert die Oberflaeche nicht mehr, auch
        nicht bei sehr vielen Dateien."""
        files = self.get_files(self.EXTENSIONS, rescan=getattr(self, "_rescan_next", True))
        self._rescan_next = True
        self.empty_lbl.hide()

        if not files:
            self.show_empty()
            if self.date_filter:
                self.empty_lbl.setText(tr(self.lang, 'date_no_results'))
            return

        self.scroll.show()
        self.lbl_loading.hide()
        self._loaded_count = 0
        self._pending_total = len(files)
        files = self._display_items(files)
        self.view.set_items(files, [self._date_for_path(p) for p in files],
                            getattr(self, "_meta", {}), self.KIND, self.lang)
        self._after_populate()

    def _display_items(self, files):
        """Lo que se muestra en la grilla (las fotos agrupan las secuencias)."""
        return files

    def _after_populate(self):
        pass

    def playlist(self):
        """Lo que se recorre con anterior/siguiente en el visor: lo mismo que
        se ve en la galeria, en el mismo orden (respeta el filtro de fecha)."""
        return list(self.view.files)

    def refresh(self):
        """Vuelve a listar la carpeta y redibuja (tras un sync, cambio de carpeta,
        orden o filtro)."""
        if not self.isVisible():
            self._needs_refresh = True    # se recarga al abrir la galeria
            return
        self.empty_lbl.hide()
        self.scroll.show()
        self.populate()

class VideoGallery(GalleryBase):
    KIND = "video"
    EXTENSIONS = (".mp4",)

    def __init__(self, folder, lang="de"):
        super().__init__(folder, tr(lang, "video_gallery_title"), "", tr(lang, "no_videos_yet"), lang)
        self._needs_refresh = True   # se carga al abrirla

    def refresh(self, *a, **k):
        r = super().refresh(*a, **k)
        self._update_chapter_marks()
        self._update_gps_marks()
        return r

    def _update_gps_marks(self):
        """Mapita en los videos con GPS. Se calcula en segundo plano y queda
        en cache (la primera vez lee unas muestras de cada video)."""
        files = list(self.view.files)
        if not files:
            return
        cache = os.path.join(CONFIG_DIR, "gps_cache.json")
        th = ScanThread(0, lambda: gps_flags(files, cache))
        th.done.connect(lambda _t, flags: self._apply_gps_marks(flags))
        self._gps_thread = th
        th.start()

    def _apply_gps_marks(self, flags):
        self.view.gps = {p for p, ok in (flags or {}).items() if ok}
        self.view.viewport().update()

    def _update_chapter_marks(self):
        """Marca en la miniatura de cada parte de un video partido
        (PARTE 1/3...), en lugar del boton de arriba."""
        def scan():
            try:
                return find_chapter_groups(self.folder)
            except Exception:
                return []
        th = ScanThread(0, lambda: scan())
        th.done.connect(lambda _t, groups: self._apply_chapter_marks(groups))
        self._chapter_thread = th      # que no lo junte el recolector antes de terminar
        th.start()

    def _apply_chapter_marks(self, groups):
        marks, tips = {}, {}
        # colores en el orden en que aparecen en la galeria: dos videos
        # partidos seguidos nunca quedan del mismo color
        pos = {os.path.normcase(x): i for i, x in enumerate(self.view.files)}
        groups = sorted(groups or [], key=lambda g: min(pos.get(os.path.normcase(x), 1 << 30) for x in g["parts"]))
        for gi, g in enumerate(groups):
            state = "done" if g["done"] else ("bad" if g["damaged"] or g.get("missing") else "ok")
            n = len(g["parts"]) + len(g.get("missing") or [])
            color = _CHAPTER_COLORS[gi % len(_CHAPTER_COLORS)]
            for i, x in enumerate(g["parts"], 1):
                marks[x] = (i, n, state, color)
                key = {"ok": "chapter_tip", "done": "chapter_tip_done", "bad": "chapter_tip_bad"}[state]
                tips[x] = os.path.normpath(x) + "\n" + tr(self.lang, key, i=i, n=n)
        self.view.chapters = marks
        self.view.tooltips = tips
        self.view.viewport().update()

    def play_video(self, path):
        SoundManager.play_click()
        dlg = VideoPlayerDialog(path, gallery_dir=self.folder, lang=self.lang, parent=self,
                                playlist=self.playlist())
        dlg.exec()

class PictureGallery(GalleryBase):
    KIND = "picture"
    EXTENSIONS = (".jpg", ".png", ".jpeg")

    def __init__(self, folder, lang="de"):
        super().__init__(folder, tr(lang, "picture_gallery_title"), "", tr(lang, "no_pictures_yet"), lang)
        self._needs_refresh = True   # se carga al abrirla

        self._expanded = set()       # secuencias abiertas en la grilla (clave de la secuencia)
        self._seqs, self._seq_by_path = {}, {}

    # ---- secuencias (time-lapse / rafaga)
    def _display_items(self, files):
        seqs = find_sequences(getattr(self, "_all_files", None) or files, getattr(self, "_mtime_cache", {}))
        self._seqs = {sq["key"]: sq for sq in seqs}
        self._seq_by_path = {p: sq for sq in seqs for p in sq["frames"]}
        out, emitted = [], set()
        for p in files:
            sq = self._seq_by_path.get(p)
            if sq is None:
                parsed = parse_seq_name(os.path.basename(p))
                key = f"{os.path.normcase(os.path.dirname(p))}|{parsed[0]}" if parsed else None
                if key in self._seqs:
                    continue                  # G0120001_1.JPG: copia repetida de una foto de la secuencia
                out.append(p)
                continue
            if sq["key"] in emitted:
                continue
            emitted.add(sq["key"])
            if sq["key"] in self._expanded:
                out.extend(sq["frames"])
            else:
                out.append(sq["frames"][0])
        return out

    def _after_populate(self):
        self._apply_sequence_marks()

    def _apply_sequence_marks(self):
        covers, frames, tips = {}, {}, {}
        shown = set(self.view.files)
        order, seen = [], set()
        for p in self.view.files:
            sq = self._seq_by_path.get(p)
            if sq and sq["key"] not in seen:
                seen.add(sq["key"])
                order.append(sq["key"])
        for gi, key in enumerate(order):
            sq = self._seqs[key]
            color = _CHAPTER_COLORS[gi % len(_CHAPTER_COLORS)]
            n = len(sq["frames"])
            expanded = key in self._expanded
            cover = sq["frames"][0]
            covers[cover] = (sq["kind"], n, expanded, color)
            label = tr(self.lang, "seq_tip_burst" if sq["kind"] == "burst" else "seq_tip_timelapse", n=n,
                       dur=self._fmt_span(sq["span"]))
            if expanded:
                for i, p in enumerate(sq["frames"], 1):
                    if p in shown:
                        frames[p] = (i, n, color)
                        tips[p] = os.path.normpath(p) + "\n" + label
            else:
                tips[cover] = os.path.normpath(cover) + "\n" + label + "\n" + tr(self.lang, "seq_tip_click")
        self.view.sequences = covers
        self.view.seq_frames = frames
        self.view.tooltips = tips
        self.view.viewport().update()

    @staticmethod
    def _fmt_span(secs):
        secs = int(secs or 0)
        if secs >= 3600:
            return f"{secs // 3600} h {secs % 3600 // 60:02d} min"
        if secs >= 60:
            return f"{secs // 60} min {secs % 60:02d} s"
        return f"{secs} s"

    def sequence_for(self, path):
        return self._seq_by_path.get(path)

    def toggle_sequence(self, sq):
        """Mostrar todas las fotos de la secuencia en la grilla, o agruparlas otra vez."""
        if sq["key"] in self._expanded:
            self._expanded.discard(sq["key"])
        else:
            self._expanded.add(sq["key"])
        pos = self.view.verticalScrollBar().value()
        self._rescan_next = False
        self.populate()
        self.view.verticalScrollBar().setValue(min(pos, self.view.verticalScrollBar().maximum()))

    def open_sequence(self, sq, start=0):
        from ..ui.sequence_viewer import SequenceViewerDialog
        SoundManager.play_click()
        dlg = SequenceViewerDialog(sq, self.lang, self, start=start,
                                   accent=getattr(self.window(), "get_current_theme_color", lambda: "#00f2fe")())
        dlg.exec()
        if dlg.changed:
            self.refresh()

    def export_sequence(self, sq):
        main = self.window()
        if hasattr(main, "export_sequence"):
            main.export_sequence(sq)

    def delete_sequence(self, sq):
        frames = list(sq["frames"])
        extra = self._sequence_extras(frames)
        total = len(frames) + len(extra)
        dlg = ConfirmDeleteDialog(self, self.lang, frames[0],
                                  question=tr(self.lang, "seq_delete_question", n=len(frames)))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok = move_to_recycle_bin(frames + extra)
        finally:
            QApplication.restoreOverrideCursor()
        show_toast(self, tr(self.lang, "seq_deleted", n=total) if ok else tr(self.lang, "delete_failed",
                                                                              name=os.path.basename(frames[0])))
        self._expanded.discard(sq["key"])
        self.refresh()

    @staticmethod
    def _sequence_extras(frames):
        """El RAW (.GPR) de cada foto, si esta al lado: se borra con ella."""
        out = []
        for p in frames:
            raw = os.path.splitext(p)[0] + ".GPR"
            if os.path.exists(raw):
                out.append(raw)
        return out

    def delete_file(self, path):
        in_seq = path in self._seq_by_path
        super().delete_file(path)
        if in_seq and not os.path.exists(path):
            self._rescan_next = True
            self.populate()            # la secuencia cambio: rearmar grupos y marcas

    def _open_item(self, path):
        sq = self._seq_by_path.get(path)
        if sq is not None and sq["key"] not in self._expanded and not self._forget_missing(path):
            self.open_sequence(sq)
            return
        super()._open_item(path)

    def open_image(self, path):
        SoundManager.play_click()
        dlg = ImageViewerDialog(path, self, playlist=self.playlist(), lang=self.lang)
        dlg.exec()
