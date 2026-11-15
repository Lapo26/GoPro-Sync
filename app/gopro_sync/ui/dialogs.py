"""Dialogos: importar, respaldar, confirmar, informacion, unir partes, idioma."""
import os
import shutil
from datetime import datetime
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)
from ..env import FONT_STACK, _safe_local_dir, _valid_start_dir
from ..i18n import _human_size, tr, wrap_path
from ..winshell import show_in_explorer
from ..media import describe_media
from ..fileops import _is_same_or_inside, list_import_files, scan_backup, scan_import_source
from ..workers import ScanThread
from ..ui.widgets import ToggleSwitch, _make_language_buttons, show_toast
from ..ui.thumbs import MixedThumbProvider, ThumbGridView

class ImportPickDialog(QDialog):
    """Elegir que archivos de la tarjeta/carpeta importar: miniaturas por
    fecha con marca de ya importado / nuevo y seleccion (como en la camara)."""

    def __init__(self, parent, lang, source, target, accent, preselected=None):
        super().__init__(parent)
        self.lang, self.source, self.target = lang, source, target
        self.entries = {}
        self.all_paths = []
        self._preselected = preselected
        self.setWindowTitle(tr(lang, "import_pick_title"))
        self.setModal(True)
        self.resize(1120, 760)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1e29; }}
            QLabel {{ color: #fff; background: transparent; }}
            * {{ font-family: {FONT_STACK}; }}
            QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
            QPushButton#primary {{ background: {accent}; color: #0d0d12; border: none; font-weight: 800; }}
            QPushButton#primary:disabled {{ background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.4); }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel(tr(lang, "import_pick_title"))
        title.setStyleSheet("font-size: 19px; font-weight: 800;")
        head.addWidget(title)
        head.addStretch()
        self.btn_sel_new = QPushButton(tr(lang, "cam_select_new"))
        self.btn_sel_none = QPushButton(tr(lang, "cam_select_none"))
        self.btn_sel_new.clicked.connect(lambda: self._select(lambda e: not e["synced"] and e["size"] > 0))
        self.btn_sel_none.clicked.connect(lambda: self._select(lambda e: False))
        head.addWidget(self.btn_sel_new)
        head.addWidget(self.btn_sel_none)
        lay.addLayout(head)
        row = QHBoxLayout()
        self.lbl_summary = QLabel(tr(lang, "import_scanning"))
        self.lbl_summary.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.6);")
        hint = QLabel(tr(lang, "import_pick_hint"))
        hint.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.4);")
        self.chk_only_new = ToggleSwitch(tr(lang, "cam_only_new"))
        self.chk_only_new.set_accent(accent)
        self.chk_only_new.toggled.connect(lambda _s: self._apply_filter())
        row.addWidget(self.lbl_summary)
        row.addSpacing(16)
        row.addWidget(hint)
        row.addStretch()
        row.addWidget(self.chk_only_new)
        lay.addLayout(row)
        self.view = ThumbGridView(self)
        self.view.accent = QColor(accent)
        self.view.selectable = True
        self.view.activated.connect(self.view.toggle_selected)   # clic = elegir / quitar
        self.view.selection_changed.connect(self._update_footer)
        lay.addWidget(self.view, 1)
        foot = QHBoxLayout()
        self.lbl_sel = QLabel("")
        self.lbl_sel.setStyleSheet(f"font-size: 13px; color: {accent};")
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.clicked.connect(self.reject)
        self.btn_use = QPushButton(tr(lang, "import_pick_use"))
        self.btn_use.setObjectName("primary")
        self.btn_use.setEnabled(False)
        self.btn_use.clicked.connect(self.accept)
        foot.addWidget(self.lbl_sel)
        foot.addStretch()
        foot.addWidget(cancel)
        foot.addWidget(self.btn_use)
        lay.addLayout(foot)
        self.provider = MixedThumbProvider(lang, self)
        th = ScanThread(1, list_import_files, source, target)
        th.done.connect(self._on_listed)
        th.start()

    def _on_listed(self, _token, entries):
        if entries is None:
            self.lbl_summary.setText(tr(self.lang, "import_empty"))
            return
        self.entries = {e["path"]: e for e in entries}
        self.all_paths = [e["path"] for e in entries]
        self.view.status = {pth: ("synced" if e["synced"] else "new") for pth, e in self.entries.items()}
        self.view.tooltips = {pth: f"{e['name']}  ·  {_human_size(e['size'])}" for pth, e in self.entries.items()}
        new = sum(1 for e in entries if not e["synced"])
        self.lbl_summary.setText(tr(self.lang, "import_pick_summary", total=len(entries), new=new))
        if self._preselected is not None:
            self.view.selected = {x for x in self._preselected if x in self.entries}
        else:
            self.view.selected = {pth for pth, e in self.entries.items() if not e["synced"] and e["size"] > 0}
        self._apply_filter(first=True)

    def _apply_filter(self, first=False):
        if not self.entries:
            return
        paths = [x for x in self.all_paths if not (self.chk_only_new.isChecked() and self.entries[x]["synced"])]
        dates = [datetime.fromtimestamp(self.entries[x]["mtime"]).strftime("%Y-%m-%d") for x in paths]
        self.view.set_items(paths, dates, {}, "picture", self.lang, provider=self.provider, keep_cache=not first)
        self._update_footer()

    def _select(self, pred):
        self.view.selected = {x for x in self.view.files if pred(self.entries[x])}
        self.view.viewport().update()
        self._update_footer()

    def _update_footer(self):
        sel = [x for x in self.view.selected if x in self.entries]
        self.lbl_sel.setText(tr(self.lang, "cam_selection", n=len(sel),
                                size=_human_size(sum(self.entries[x]["size"] for x in sel))))
        self.btn_use.setEnabled(bool(sel))

    def selected_paths(self):
        return [x for x in self.all_paths if x in self.view.selected]

    def done(self, r):
        self.view.shutdown()
        super().done(r)


class ImportConfirmDialog(QDialog):
    """Paso 2 de "Importar SD / carpeta": resumen + origen y destino
    cambiables antes de empezar."""

    def __init__(self, parent, lang, source, target, accent):
        super().__init__(parent)
        self.lang = lang
        self.source = source
        self.target = target
        self.accent = accent
        self.only_paths = None       # None = todo lo nuevo; lista = solo esos archivos
        self.setWindowTitle(tr(lang, "import_confirm_title"))
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1e29; }}
            QLabel {{ color: #fff; background: transparent; }}
            * {{ font-family: {FONT_STACK}; }}
            QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
            QPushButton#primary {{ background: {accent}; color: #0d0d12; border: none; font-weight: 800; }}
            QPushButton#primary:disabled {{ background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.4); }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(12)

        title = QLabel(tr(lang, "import_confirm_title"))
        title.setStyleSheet("font-size: 19px; font-weight: 800;")
        lay.addWidget(title)

        path_style = ("color: rgba(255,255,255,0.8); font-size: 12px; background: rgba(255,255,255,0.05);"
                      " border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 8px 10px;")
        self.lbl_source = QLabel()
        self.lbl_target = QLabel()
        for caption_key, lbl, handler in (("import_from", self.lbl_source, self._change_source),
                                          ("import_to", self.lbl_target, self._change_target)):
            cap = QLabel(tr(lang, caption_key))
            cap.setStyleSheet("font-size: 12px; font-weight: 800; color: rgba(255,255,255,0.5); letter-spacing: 1px;")
            lay.addWidget(cap)
            row = QHBoxLayout()
            lbl.setStyleSheet(path_style)
            lbl.setWordWrap(True)
            btn = QPushButton(tr(lang, "import_change"))
            btn.clicked.connect(handler)
            row.addWidget(lbl, 1)
            row.addWidget(btn)
            lay.addLayout(row)

        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("font-size: 15px; font-weight: 700; margin-top: 6px;")
        self.lbl_new = QLabel()
        self.lbl_new.setStyleSheet(f"font-size: 13px; color: {accent};")
        self.lbl_new.setWordWrap(True)
        lay.addWidget(self.lbl_summary)
        lay.addWidget(self.lbl_new)

        buttons = QHBoxLayout()
        self.btn_pick = QPushButton(tr(lang, "import_pick_btn"))
        self.btn_pick.setEnabled(False)
        self.btn_pick.clicked.connect(self._pick_files)
        buttons.addWidget(self.btn_pick)
        buttons.addStretch()
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton(tr(lang, "import_btn"))
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.btn_ok)
        lay.addLayout(buttons)
        self._refresh()

    def _refresh(self):
        self.lbl_source.setText(wrap_path(self.source))
        self.lbl_target.setText(wrap_path(self.target))
        self.lbl_summary.setText(tr(self.lang, "import_scanning"))
        self.lbl_new.setText("")
        self.btn_ok.setEnabled(False)
        self.btn_pick.setEnabled(False)
        self.only_paths = None           # cambio el origen o el destino: la eleccion anterior no vale
        # Contar en segundo plano: la ventana sigue respondiendo (se puede
        # cancelar o cambiar la carpeta mientras tanto).
        self._scan_token = getattr(self, "_scan_token", 0) + 1
        th = ScanThread(self._scan_token, scan_import_source, self.source, self.target)
        th.done.connect(self._on_scanned)
        th.start()

    def _on_scanned(self, token, info):
        if token != self._scan_token:
            return                  # resultado viejo: se cambio la carpeta o el destino
        if info is None:
            self.lbl_summary.setText(tr(self.lang, "import_empty"))
            return
        self.info = info
        if info["photos"] + info["videos"] == 0:
            self.lbl_summary.setText(tr(self.lang, "import_empty"))
            return
        self.btn_pick.setEnabled(True)
        self.lbl_summary.setText(tr(self.lang, "import_summary", photos=info["photos"], videos=info["videos"],
                                    size=_human_size(info["bytes"])))
        if info["new"] == 0:
            self.lbl_new.setText(tr(self.lang, "import_nothing_new", n=info["existing"]))
        else:
            self.lbl_new.setText(tr(self.lang, "import_new_summary", new=info["new"], existing=info["existing"])
                                 + f"  ({_human_size(info['new_bytes'])})")
            self.btn_ok.setEnabled(True)

    def _change_source(self):
        d = QFileDialog.getExistingDirectory(self, tr(self.lang, "import_title"), _valid_start_dir(self.source))
        if d:
            self.source = os.path.normpath(d)
            self._refresh()

    def _change_target(self):
        d = QFileDialog.getExistingDirectory(self, tr(self.lang, "choose_target_dir_title"), _valid_start_dir(self.target))
        if d:
            self.target = os.path.normpath(d)
            self._refresh()

    def _pick_files(self):
        dlg = ImportPickDialog(self, self.lang, self.source, self.target, self.accent, preselected=self.only_paths)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.only_paths = dlg.selected_paths()
        size = sum(dlg.entries[x]["size"] for x in self.only_paths)
        self.lbl_new.setText(tr(self.lang, "import_picked", n=len(self.only_paths), size=_human_size(size)))
        self.btn_ok.setEnabled(bool(self.only_paths))

    def accept(self):
        if _is_same_or_inside(self.source, self.target) or _is_same_or_inside(self.target, self.source):
            show_toast(self, tr(self.lang, "import_same_as_target"))
            return
        super().accept()


class BackupDialog(QDialog):
    """Respaldar a disco: confirmar carpeta a respaldar y destino (ambos
    cambiables) con un resumen. La copia corre despues en Actividad."""

    def __init__(self, parent, lang, source, dest, accent, remember_default=True, verify_default=False):
        super().__init__(parent)
        self.lang = lang
        self.source = source
        self.dest = dest or ""
        self.setWindowTitle(tr(lang, "backup_dialog_title"))
        self.setModal(True)
        self.setMinimumWidth(580)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1e29; }}
            QLabel {{ color: #fff; background: transparent; }}
            * {{ font-family: {FONT_STACK}; }}
            QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
            QPushButton#primary {{ background: {accent}; color: #0d0d12; border: none; font-weight: 800; }}
            QPushButton#primary:disabled {{ background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.4); }}
            QProgressBar {{ background: rgba(255,255,255,0.08); border: none; border-radius: 8px; height: 16px;
                           text-align: center; color: #fff; font-weight: 700; }}
            QProgressBar::chunk {{ background: {accent}; border-radius: 8px; }}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 20)
        outer.setSpacing(12)
        title = QLabel(tr(lang, "backup_dialog_title"))
        title.setStyleSheet("font-size: 19px; font-weight: 800;")
        outer.addWidget(title)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)

        # ---- pagina 1: confirmacion
        p1 = QWidget()
        lay = QVBoxLayout(p1)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        path_style = ("color: rgba(255,255,255,0.8); font-size: 12px; background: rgba(255,255,255,0.05);"
                      " border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 8px 10px;")
        self.lbl_source = QLabel()
        self.lbl_dest = QLabel()
        for caption_key, lbl, handler in (("backup_from", self.lbl_source, self._change_source),
                                          ("backup_to", self.lbl_dest, self._change_dest)):
            cap = QLabel(tr(lang, caption_key))
            cap.setStyleSheet("font-size: 12px; font-weight: 800; color: rgba(255,255,255,0.5); letter-spacing: 1px;")
            lay.addWidget(cap)
            row = QHBoxLayout()
            lbl.setStyleSheet(path_style)
            lbl.setWordWrap(True)
            btn = QPushButton(tr(lang, "import_change"))
            btn.clicked.connect(handler)
            row.addWidget(lbl, 1)
            row.addWidget(btn)
            lay.addLayout(row)
        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("font-size: 15px; font-weight: 700; margin-top: 6px;")
        self.lbl_pending = QLabel()
        self.lbl_pending.setWordWrap(True)
        self.lbl_pending.setStyleSheet(f"font-size: 13px; color: {accent};")
        lay.addWidget(self.lbl_summary)
        lay.addWidget(self.lbl_pending)
        self.chk_remember = ToggleSwitch(tr(lang, "backup_remember"))
        self.chk_remember.set_accent(accent)
        self.chk_remember.setChecked(remember_default, animate=False)
        lay.addWidget(self.chk_remember)
        self.chk_verify = ToggleSwitch(tr(lang, "backup_verify"))
        self.chk_verify.set_accent(accent)
        self.chk_verify.setChecked(verify_default, animate=False)
        lay.addWidget(self.chk_verify)
        note = QLabel(tr(lang, "backup_hint"))
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 11px; color: #888;")
        lay.addWidget(note)
        b1 = QHBoxLayout()
        b1.addStretch()
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.clicked.connect(self.reject)
        self.btn_start = QPushButton(tr(lang, "backup_start_btn"))
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start)
        b1.addWidget(cancel)
        b1.addWidget(self.btn_start)
        lay.addLayout(b1)
        self.pages.addWidget(p1)

        self._refresh()

    # ---- confirmacion
    def _refresh(self):
        self.lbl_source.setText(wrap_path(self.source))
        self.lbl_dest.setText(wrap_path(self.dest) if self.dest else "—")
        self.btn_start.setEnabled(False)
        if not self.dest:
            self.lbl_summary.setText("")
            self.lbl_pending.setText(tr(self.lang, "backup_dest_missing"))
            return
        self.lbl_summary.setText(tr(self.lang, "import_scanning"))
        self.lbl_pending.setText("")
        self._scan_token = getattr(self, "_scan_token", 0) + 1
        th = ScanThread(self._scan_token, scan_backup, self.source, self.dest)
        th.done.connect(self._on_scanned)
        th.start()

    def _on_scanned(self, token, info):
        if token != self._scan_token:
            return                  # resultado viejo: se cambio la carpeta o el destino
        if info is None:
            self.lbl_summary.setText("")
            return
        self.lbl_summary.setText(tr(self.lang, "backup_summary", total=info["total"], size=_human_size(info["bytes"])))
        if info["pending"] == 0:
            self.lbl_pending.setText(tr(self.lang, "backup_up_to_date"))
        else:
            self.lbl_pending.setText(tr(self.lang, "backup_pending", n=info["pending"],
                                        size=_human_size(info["pending_bytes"]), skipped=info["skipped"]))
            self.btn_start.setEnabled(True)

    def _change_source(self):
        d = QFileDialog.getExistingDirectory(self, tr(self.lang, "backup_choose_source_title"), _valid_start_dir(self.source))
        if d:
            self.source = os.path.normpath(d)
            self._refresh()

    def _change_dest(self):
        d = QFileDialog.getExistingDirectory(self, tr(self.lang, "backup_choose_title"),
                                             _valid_start_dir(self.dest) if self.dest else _safe_local_dir())
        if d:
            self.dest = os.path.normpath(d)
            self._refresh()

    def _start(self):
        if _is_same_or_inside(self.dest, self.source) or _is_same_or_inside(self.source, self.dest):
            show_toast(self, tr(self.lang, "backup_same_as_target"))
            return
        self.accept()      # la copia corre en segundo plano (Actividad)

    def remember(self):
        return self.chk_remember.isChecked()


class ConfirmDeleteDialog(QDialog):
    """Confirmacion antes de eliminar (con el estilo de la app)."""

    def __init__(self, parent, lang, path, question=None):
        super().__init__(parent)
        self.setWindowTitle(tr(lang, "delete_title"))
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setStyleSheet(
            f"QDialog {{ background: #1c1e29; }} QLabel {{ color: #fff; background: transparent; }}"
            f" * {{ font-family: {FONT_STACK}; }}"
            " QPushButton { background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);"
            " border-radius: 10px; padding: 8px 18px; font-weight: 600; }"
            " QPushButton:hover { background: rgba(255,255,255,0.16); }"
            " QPushButton#danger { background: #e5484d; border: none; font-weight: 800; }"
            " QPushButton#danger:hover { background: #f2555a; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        q = QLabel(question or tr(lang, "delete_question", name=os.path.basename(path)))
        q.setWordWrap(True)
        q.setStyleSheet("font-size: 17px; font-weight: 800;")
        d = QLabel(tr(lang, "delete_detail"))
        d.setWordWrap(True)
        d.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
        where = QLabel(wrap_path(os.path.dirname(path)))
        where.setWordWrap(True)
        where.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.45);")
        lay.addWidget(q)
        lay.addWidget(d)
        lay.addWidget(where)
        lay.addSpacing(8)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.setDefault(True)               # Enter = cancelar (lo seguro)
        cancel.clicked.connect(self.reject)
        ok = QPushButton(tr(lang, "delete_btn"))
        ok.setObjectName("danger")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)


class ConfirmQuitDialog(QDialog):
    """Salir con una copia en curso: confirmar (con el estilo de la app)."""

    def __init__(self, parent, lang):
        super().__init__(parent)
        self.setWindowTitle(tr(lang, "quit_busy_title"))
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setStyleSheet(
            f"QDialog {{ background: #1c1e29; }} QLabel {{ color: #fff; background: transparent; }}"
            f" * {{ font-family: {FONT_STACK}; }}"
            " QPushButton { background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);"
            " border-radius: 10px; padding: 8px 18px; font-weight: 600; }"
            " QPushButton:hover { background: rgba(255,255,255,0.16); }"
            " QPushButton#danger { background: #e5484d; border: none; font-weight: 800; }"
            " QPushButton#danger:hover { background: #f2555a; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        q = QLabel(tr(lang, "quit_busy_question"))
        q.setWordWrap(True)
        q.setStyleSheet("font-size: 17px; font-weight: 800;")
        d = QLabel(tr(lang, "quit_busy_detail"))
        d.setWordWrap(True)
        d.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
        lay.addWidget(q)
        lay.addWidget(d)
        lay.addSpacing(8)
        row = QHBoxLayout()
        row.addStretch()
        keep = QPushButton(tr(lang, "quit_busy_keep"))
        keep.setDefault(True)                 # Enter = seguir copiando (lo seguro)
        keep.clicked.connect(self.reject)
        ok = QPushButton(tr(lang, "quit_busy_quit"))
        ok.setObjectName("danger")
        ok.clicked.connect(self.accept)
        row.addWidget(keep)
        row.addWidget(ok)
        lay.addLayout(row)


class MediaInfoDialog(QDialog):
    """Ficha de informacion de una foto/video (clic derecho > Informacion)."""

    def __init__(self, parent, lang, path, accent):
        super().__init__(parent)
        self.setWindowTitle(tr(lang, "info_title"))
        self.setMinimumWidth(560)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1e29; }}
            QLabel {{ color: #fff; background: transparent; }}
            * {{ font-family: {FONT_STACK}; }}
            QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
            QPushButton#primary {{ background: {accent}; color: #0d0d12; border: none; font-weight: 800; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(6)
        t = QLabel(os.path.basename(path))
        t.setStyleSheet("font-size: 18px; font-weight: 800;")
        lay.addWidget(t)
        current = None
        grid = None
        for section, label, val in describe_media(path, lang):
            if section != current:
                current = section
                cap = QLabel(tr(lang, "info_file" if section == "file" else "info_media"))
                cap.setStyleSheet("font-size: 11px; font-weight: 800; letter-spacing: 1px; color: rgba(255,255,255,0.45);"
                                  " margin-top: 10px;")
                lay.addWidget(cap)
                grid = QGridLayout()
                grid.setHorizontalSpacing(16)
                grid.setVerticalSpacing(5)
                grid.setColumnStretch(1, 1)
                grid.setColumnMinimumWidth(0, 130)   # mismas columnas en ambas secciones
                lay.addLayout(grid)
            r = grid.rowCount()
            k = QLabel(label)
            k.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.55);")
            v = QLabel(wrap_path(val) if label == tr(lang, "info_path") else val)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setStyleSheet("font-size: 13px;")
            grid.addWidget(k, r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v, r, 1)
        lay.addSpacing(10)
        row = QHBoxLayout()
        b_copy = QPushButton(tr(lang, "ctx_copy_path"))
        b_copy.clicked.connect(lambda: (QApplication.clipboard().setText(os.path.normpath(path)),
                                        show_toast(self, tr(lang, "path_copied"))))
        b_show = QPushButton(tr(lang, "ctx_show_in_explorer"))
        b_show.clicked.connect(lambda: show_in_explorer(path))
        b_close = QPushButton(tr(lang, "close_btn2"))
        b_close.setObjectName("primary")
        b_close.clicked.connect(self.accept)
        row.addWidget(b_copy)
        row.addWidget(b_show)
        row.addStretch()
        row.addWidget(b_close)
        lay.addLayout(row)


class LanguageDialog(QDialog):
    """Se muestra una vez en instalaciones que ya habian hecho el asistente
    antes de que existiera la eleccion de idioma (quedaban en aleman)."""

    def __init__(self, parent, current, accent):
        super().__init__(parent)
        self.chosen = current
        self.setWindowTitle("GoPro Sync Pro")
        self.setModal(True)
        self.setMinimumWidth(380)
        self.setStyleSheet(f"QDialog {{ background: #1c1e29; }} QLabel {{ color: #fff; background: transparent; }}"
                           f" * {{ font-family: {FONT_STACK}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(12)
        title = QLabel("Idioma · Language · Sprache")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: 800;")
        lay.addWidget(title)
        box, _ = _make_language_buttons(current, accent, self._pick)
        lay.addLayout(box)

    def _pick(self, code):
        self.chosen = code
        self.accept()


class JoinDialog(QDialog):
    """Lista los videos partidos y los une (todos o los elegidos)."""

    def __init__(self, parent, lang, groups, accent):
        super().__init__(parent)
        self.lang = lang
        self.groups = [g for g in groups if not g["done"]]
        self.setWindowTitle(tr(lang, "join_title"))
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1e29; }}
            QLabel {{ color: #fff; background: transparent; }}
            * {{ font-family: {FONT_STACK}; }}
            QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
            QPushButton#primary {{ background: {accent}; color: #0d0d12; border: none; font-weight: 800; }}
            QPushButton#primary:disabled {{ background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.4); }}
            QScrollArea {{ border: none; background: transparent; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        title = QLabel(tr(lang, "join_title"))
        title.setStyleSheet("font-size: 19px; font-weight: 800;")
        intro = QLabel(tr(lang, "join_intro"))
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
        lay.addWidget(title)
        lay.addWidget(intro)
        # Lista con casillas: se puede recorrer toda (rueda o barra) y marcar cada video
        self.list = QListWidget()
        self.list.setStyleSheet(
            f"QListWidget {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10);"
            f" border-radius: 10px; padding: 4px; color: #fff; font-size: 13px; outline: none; }}"
            f" QListWidget::item {{ padding: 7px 6px; border-radius: 6px; }}"
            f" QListWidget::item:hover {{ background: rgba(255,255,255,0.06); }}"
            f" QListWidget::item:selected {{ background: rgba(255,255,255,0.08); color: #fff; }}"
            f" QListWidget::indicator {{ width: 18px; height: 18px; border-radius: 5px;"
            f" border: 2px solid rgba(255,255,255,0.45); background: transparent; }}"
            f" QListWidget::indicator:checked {{ background: {accent}; border-color: {accent}; }}")
        self.list.setMinimumHeight(260)
        self.items = []
        for g in self.groups:
            folder = os.path.basename(os.path.dirname(g["parts"][0]))
            name = f"{folder} · {g['name']}"
            if g["damaged"] or g.get("missing"):
                it = QListWidgetItem(tr(lang, "join_damaged", name=name, part=", ".join(g["damaged"])) if g["damaged"]
                                     else tr(lang, "join_missing", name=name, part=", ".join(g["missing"])))
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                it.setForeground(QColor("#ff8a8a"))
                self.list.addItem(it)
                continue
            it = QListWidgetItem(tr(lang, "join_group", name=name, n=len(g["parts"]), size=_human_size(g["size"])))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            it.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(it)
            self.items.append((it, g))
        if not self.groups:
            it = QListWidgetItem(tr(lang, "join_none"))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(it)
        self.list.itemChanged.connect(lambda _i: self._update())
        lay.addWidget(self.list, 1)
        sel_row = QHBoxLayout()
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet(f"font-size: 12px; color: {accent};")
        btn_all = QPushButton(tr(lang, "select_all"))
        btn_none = QPushButton(tr(lang, "cam_select_none"))
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(self.lbl_count)
        sel_row.addStretch()
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        lay.addLayout(sel_row)
        self.chk_recycle = ToggleSwitch(tr(lang, "join_recycle"))
        self.chk_recycle.set_accent(accent)
        lay.addWidget(self.chk_recycle)
        self.lbl_space = QLabel("")
        self.lbl_space.setWordWrap(True)
        self.lbl_space.setStyleSheet("font-size: 12px; color: #ff8a8a;")
        lay.addWidget(self.lbl_space)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.btn_ok)
        lay.addLayout(row)
        self._update()

    def chosen(self):
        return [g for it, g in self.items if it.checkState() == Qt.CheckState.Checked]

    def _set_all(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.list.blockSignals(True)
        for it, _g in self.items:
            it.setCheckState(state)
        self.list.blockSignals(False)
        self._update()

    def _update(self):
        sel = self.chosen()
        self.lbl_count.setText(tr(self.lang, "join_selected", n=len(sel), total=len(self.items),
                                  size=_human_size(sum(g["size"] for g in sel))))
        self.btn_ok.setText(tr(self.lang, "join_start", n=len(sel)))
        need = sum(g["size"] for g in sel)
        ok = bool(sel)
        self.lbl_space.setText("")
        if sel:
            try:
                free = shutil.disk_usage(os.path.dirname(sel[0]["parts"][0])).free
                if free < need * 1.02:
                    self.lbl_space.setText(tr(self.lang, "join_space", need=_human_size(need), free=_human_size(free)))
                    ok = False
            except Exception:
                pass
        self.btn_ok.setEnabled(ok)


class SequenceExportDialog(QDialog):
    """Opciones para convertir un time-lapse o una rafaga en video (MP4) o GIF."""
    MP4_WIDTHS = ((3840, "seq_res_4k"), (1920, "seq_res_1080"), (1280, "seq_res_720"))
    GIF_WIDTHS = ((720, "seq_gif_720"), (480, "seq_gif_480"), (360, "seq_gif_360"))
    FPS = (5, 10, 15, 24, 30, 60)

    def __init__(self, parent, lang, seq, accent, last=None):
        from PySide6.QtWidgets import QComboBox
        super().__init__(parent)
        self.lang, self.seq = lang, seq
        burst = seq.get("kind") == "burst"
        last = dict(last or {})
        self.out_dir = seq.get("dir") or ""
        self.setWindowTitle(tr(lang, "seq_export_title"))
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1e29; }}
            QLabel {{ color: #fff; background: transparent; }}
            * {{ font-family: {FONT_STACK}; }}
            QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                          border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
            QPushButton#primary {{ background: {accent}; color: #0d0d12; border: none; font-weight: 800; }}
            QComboBox {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                        border-radius: 8px; padding: 6px 10px; min-width: 220px; }}
            QComboBox QAbstractItemView {{ background: #23232b; color: #fff; selection-background-color: rgba(255,255,255,0.16); }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        title = QLabel(tr(lang, "seq_export_title"))
        title.setStyleSheet("font-size: 19px; font-weight: 800;")
        lay.addWidget(title)
        intro = QLabel(tr(lang, "seq_export_intro_burst" if burst else "seq_export_intro", n=len(seq["frames"])))
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
        lay.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        self.cmb_format = QComboBox()
        self.cmb_format.addItem(tr(lang, "seq_fmt_mp4"), "mp4")
        self.cmb_format.addItem(tr(lang, "seq_fmt_gif"), "gif")
        self.cmb_res = QComboBox()
        self.cmb_crop = QComboBox()
        self.cmb_crop.addItem(tr(lang, "seq_crop_none"), False)
        self.cmb_crop.addItem(tr(lang, "seq_crop_169"), True)
        self.cmb_fps = QComboBox()
        for f in self.FPS:
            self.cmb_fps.addItem(tr(lang, "seq_fps", n=f), f)
        rows = (("seq_opt_format", self.cmb_format), ("seq_opt_res", self.cmb_res),
                ("seq_opt_crop", self.cmb_crop), ("seq_opt_fps", self.cmb_fps))
        for r, (key, w) in enumerate(rows):
            lbl = QLabel(tr(lang, key))
            lbl.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.8);")
            grid.addWidget(lbl, r, 0)
            grid.addWidget(w, r, 1)
        lay.addLayout(grid)
        self.lbl_dur = QLabel("")
        self.lbl_dur.setStyleSheet(f"font-size: 12px; color: {accent};")
        lay.addWidget(self.lbl_dur)
        self.chk_deflicker = ToggleSwitch(tr(lang, "seq_opt_deflicker"))
        self.chk_deflicker.set_accent(accent)
        lay.addWidget(self.chk_deflicker)
        hint = QLabel(tr(lang, "seq_deflicker_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.45);")
        lay.addWidget(hint)
        self._deflicker_hint = hint

        dest_row = QHBoxLayout()
        self.lbl_dest = QLabel("")
        self.lbl_dest.setWordWrap(True)
        self.lbl_dest.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.55);")
        btn_dest = QPushButton(tr(lang, "seq_dest_change"))
        btn_dest.clicked.connect(self._pick_dest)
        dest_row.addWidget(self.lbl_dest, 1)
        dest_row.addWidget(btn_dest)
        lay.addSpacing(4)
        lay.addLayout(dest_row)
        lay.addSpacing(8)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.clicked.connect(self.reject)
        ok = QPushButton(tr(lang, "seq_export_go"))
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

        # valores iniciales: lo ultimo que se uso para este tipo de secuencia
        fmt = last.get("format") or ("gif" if burst and len(seq["frames"]) <= 60 else "mp4")
        self.cmb_format.setCurrentIndex(1 if fmt == "gif" else 0)
        self._fill_res(last.get("width"))
        self.cmb_crop.setCurrentIndex(1 if last.get("crop169") else 0)
        fps = last.get("fps") or (10 if burst else 30)
        self.cmb_fps.setCurrentIndex(self.FPS.index(fps) if fps in self.FPS else 4)
        self.chk_deflicker.setChecked(last.get("deflicker", not burst), animate=False)
        self.cmb_format.currentIndexChanged.connect(lambda _i: (self._fill_res(None), self._update()))
        self.cmb_fps.currentIndexChanged.connect(lambda _i: self._update())
        self._update()

    def _fill_res(self, want):
        gif = self.cmb_format.currentData() == "gif"
        self.cmb_res.clear()
        for w, key in (self.GIF_WIDTHS if gif else self.MP4_WIDTHS):
            self.cmb_res.addItem(tr(self.lang, key), w)
        idx = self.cmb_res.findData(want) if want else -1
        self.cmb_res.setCurrentIndex(idx if idx >= 0 else (1 if gif else 1))

    def _update(self):
        gif = self.cmb_format.currentData() == "gif"
        self.chk_deflicker.setVisible(not gif)
        self._deflicker_hint.setVisible(not gif)
        secs = len(self.seq["frames"]) / float(self.cmb_fps.currentData() or 30)
        self.lbl_dur.setText(tr(self.lang, "seq_result_dur", dur=f"{int(secs // 60)}:{secs % 60:04.1f}"))
        self.lbl_dest.setText(tr(self.lang, "seq_dest", path=wrap_path(self.out_dir, 52)))

    def _pick_dest(self):
        d = QFileDialog.getExistingDirectory(self, tr(self.lang, "seq_dest_change"), _valid_start_dir(self.out_dir))
        if d:
            self.out_dir = os.path.normpath(d)
            self._update()

    def options(self):
        return {"format": self.cmb_format.currentData(), "width": self.cmb_res.currentData(),
                "crop169": bool(self.cmb_crop.currentData()), "fps": self.cmb_fps.currentData(),
                "deflicker": self.chk_deflicker.isChecked()}
