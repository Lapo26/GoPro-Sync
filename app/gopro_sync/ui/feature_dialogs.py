"""Dialogos de las funciones nuevas: exportar momentos, video con datos,
camara lenta / acelerado, etiquetas, plantilla de carpetas."""
import os
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout)
from ..env import _valid_start_dir
from ..i18n import tr, wrap_path
from ..ui.widgets import ToggleSwitch
from ..ui.theme import dialog_css


def dialog_style(accent):
    return dialog_css(accent, """
        QComboBox, QLineEdit, QAbstractSpinBox { min-width: 200px; }
    """)


class OptionsDialog(QDialog):
    """Base: titulo, explicacion, opciones en grilla, carpeta de destino y botones."""

    def __init__(self, parent, lang, accent, title, intro, go_key, out_dir=None):
        super().__init__(parent)
        self.lang, self.accent = lang, accent
        self.out_dir = out_dir
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(540)
        self.setStyleSheet(dialog_style(accent))
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(26, 22, 26, 20)
        self.lay.setSpacing(10)
        t = QLabel(title)
        t.setStyleSheet("font-size: 19px; font-weight: 800;")
        self.lay.addWidget(t)
        if intro:
            i = QLabel(intro)
            i.setWordWrap(True)
            i.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
            self.lay.addWidget(i)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(10)
        self.lay.addLayout(self.grid)
        self._rows = 0
        self.extra = QVBoxLayout()
        self.extra.setSpacing(8)
        self.lay.addLayout(self.extra)
        self.lbl_note = QLabel("")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet(f"font-size: 12px; color: {accent};")
        self.lbl_note.hide()
        self.lay.addWidget(self.lbl_note)
        if out_dir is not None:
            row = QHBoxLayout()
            self.lbl_dest = QLabel("")
            self.lbl_dest.setWordWrap(True)
            self.lbl_dest.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.55);")
            b = QPushButton(tr(lang, "seq_dest_change"))
            b.clicked.connect(self._pick_dest)
            row.addWidget(self.lbl_dest, 1)
            row.addWidget(b)
            self.lay.addSpacing(4)
            self.lay.addLayout(row)
            self._show_dest()
        self.lay.addSpacing(8)
        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton(tr(lang, go_key))
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(self.btn_ok)
        self.lay.addLayout(btns)

    def add_row(self, key, widget):
        lbl = QLabel(tr(self.lang, key))
        lbl.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.8);")
        self.grid.addWidget(lbl, self._rows, 0)
        self.grid.addWidget(widget, self._rows, 1)
        self._rows += 1
        return widget

    def combo(self, key, items, current=None):
        c = QComboBox()
        for label, data in items:
            c.addItem(label, data)
        if current is not None:
            i = c.findData(current)
            if i >= 0:
                c.setCurrentIndex(i)
        return self.add_row(key, c)

    def toggle(self, key, checked):
        t = ToggleSwitch(tr(self.lang, key))
        t.set_accent(self.accent)
        t.setChecked(bool(checked), animate=False)
        self.extra.addWidget(t)
        return t

    def note(self, text):
        self.lbl_note.setText(text)
        self.lbl_note.setVisible(bool(text))

    def _show_dest(self):
        self.lbl_dest.setText(tr(self.lang, "seq_dest", path=wrap_path(self.out_dir or "", 52)))

    def _pick_dest(self):
        d = QFileDialog.getExistingDirectory(self, tr(self.lang, "seq_dest_change"), _valid_start_dir(self.out_dir))
        if d:
            self.out_dir = os.path.normpath(d)
            self._show_dest()


def _clip_modes(lang):
    return [(tr(lang, "clip_mode_share"), "share"), (tr(lang, "clip_mode_hd"), "hd"),
            (tr(lang, "clip_mode_original"), "original")]


class HiLightExportDialog(OptionsDialog):
    """Exportar los momentos: segundos antes / despues, calidad y si van en un
    solo video resumen o en archivos separados."""

    def __init__(self, parent, lang, accent, n_moments, out_dir, last=None, n_videos=1):
        last = dict(last or {})
        intro = tr(lang, "hl_export_intro", n=n_moments) if n_videos == 1 else \
            tr(lang, "hl_export_intro_many", n=n_moments, v=n_videos)
        super().__init__(parent, lang, accent, tr(lang, "hl_export_title"), intro, "hl_export_go", out_dir)
        self.sp_before = QDoubleSpinBox()
        self.sp_after = QDoubleSpinBox()
        for sp, val in ((self.sp_before, last.get("before", 5)), (self.sp_after, last.get("after", 5))):
            sp.setRange(0, 60)
            sp.setDecimals(0)
            sp.setSuffix(" s")
            sp.setValue(val)
        self.add_row("hl_before", self.sp_before)
        self.add_row("hl_after", self.sp_after)
        self.cmb_mode = self.combo("hl_quality", _clip_modes(lang), last.get("mode", "hd"))
        self.chk_single = self.toggle("hl_single", last.get("single", True))

    def options(self):
        return {"before": self.sp_before.value(), "after": self.sp_after.value(),
                "mode": self.cmb_mode.currentData(), "single": self.chk_single.isChecked()}


class OverlayDialog(OptionsDialog):
    """Video con los datos encima: que mostrar, unidades y resolucion."""

    def __init__(self, parent, lang, accent, src_height, out_dir, last=None):
        last = dict(last or {})
        super().__init__(parent, lang, accent, tr(lang, "ov_title"), tr(lang, "ov_intro"), "ov_go", out_dir)
        heights = [(tr(lang, "ov_res_orig", h=src_height), src_height)]
        for h, key in ((1080, "seq_res_1080"), (720, "seq_res_720")):
            if h < src_height:
                heights.append((tr(lang, key), h))
        want = last.get("height", 1080 if src_height > 1080 else src_height)
        self.cmb_res = self.combo("ov_res", heights, want if any(h == want for _l, h in heights) else src_height)
        self.cmb_units = self.combo("ov_units", [("km/h · m", "kmh"), ("mph · ft", "mph")], last.get("units", "kmh"))
        self.chk = {}
        for key in ("speed", "alt", "dist", "clock", "map"):
            self.chk[key] = self.toggle("ov_show_" + key, last.get(key, True))
        self.note(tr(lang, "ov_note"))

    def options(self):
        out = {k: t.isChecked() for k, t in self.chk.items()}
        out.update(height=self.cmb_res.currentData(), units=self.cmb_units.currentData(), accent=self.accent)
        return out


class SpeedDialog(OptionsDialog):
    """Camara lenta o video acelerado."""

    def __init__(self, parent, lang, accent, paths, out_dir, last=None):
        from ..videotools import SPEED_FACTORS, speed_plan
        self._plan = speed_plan
        self.paths = paths
        intro = tr(lang, "speed_intro") if len(paths) == 1 else tr(lang, "speed_intro_many", n=len(paths))
        super().__init__(parent, lang, accent, tr(lang, "speed_title"), intro, "speed_go", out_dir)
        items = []
        for f in SPEED_FACTORS:
            key = "speed_slow" if f < 1 else "speed_fast"
            items.append((tr(lang, key, f=(f"1/{int(round(1 / f))}" if f < 1 else f"{f:g}")), f))
        self.cmb = self.combo("speed_factor", items, (last or {}).get("factor", 0.25))
        self.cmb.currentIndexChanged.connect(lambda _i: self._update())
        self._update()

    def _update(self):
        f = self.cmb.currentData()
        try:
            mode, fps, dur = self._plan(self.paths[0], f)
        except Exception:
            self.note("")
            return
        out = dur / f if dur else 0
        txt = tr(self.lang, "speed_note_copy" if mode == "copy" else "speed_note_encode", fps=f"{fps:.0f}",
                 dur=f"{int(out // 60)}:{int(out % 60):02d}")
        if len(self.paths) > 1:
            txt = tr(self.lang, "speed_note_first") + " " + txt
        self.note(txt)

    def factor(self):
        return self.cmb.currentData()


class TagsDialog(QDialog):
    """Etiquetas de uno o varios archivos: escribir separadas por coma o
    elegir de las que ya se usaron."""

    def __init__(self, parent, lang, accent, current, known, n_files=1):
        super().__init__(parent)
        self.lang = lang
        self.setWindowTitle(tr(lang, "tags_title"))
        self.setMinimumWidth(480)
        self.setStyleSheet(dialog_style(accent))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        t = QLabel(tr(lang, "tags_title"))
        t.setStyleSheet("font-size: 19px; font-weight: 800;")
        lay.addWidget(t)
        i = QLabel(tr(lang, "tags_intro") if n_files == 1 else tr(lang, "tags_intro_many", n=n_files))
        i.setWordWrap(True)
        i.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
        lay.addWidget(i)
        self.edit = QLineEdit(", ".join(current))
        self.edit.setPlaceholderText(tr(lang, "tags_placeholder"))
        lay.addWidget(self.edit)
        if known:
            row = QHBoxLayout()
            row.setSpacing(6)
            for tag in known[:12]:
                b = QPushButton(tag)
                b.setStyleSheet("padding: 4px 10px; font-size: 12px;")
                b.clicked.connect(lambda _c=False, tg=tag: self._add(tg))
                row.addWidget(b)
            row.addStretch()
            lay.addLayout(row)
        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr(lang, "cancel_btn"))
        cancel.clicked.connect(self.reject)
        ok = QPushButton(tr(lang, "tags_save"))
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def _add(self, tag):
        cur = self.tags()
        if tag.lower() not in [c.lower() for c in cur]:
            cur.append(tag)
        self.edit.setText(", ".join(cur))

    def tags(self):
        return [x.strip() for x in self.edit.text().replace(";", ",").split(",") if x.strip()]


class FolderTemplateDialog(OptionsDialog):
    """Como se arman las carpetas al copiar: plantilla con fecha, camara y lugar."""
    PRESETS = ("{AAAA}-{MM}-{DD}", "{AAAA}/{AAAA}-{MM}-{DD}", "{AAAA}/{MM}/{DD}", "{AAAA}-{MM}-{DD} {lugar}",
               "{AAAA}/{AAAA}-{MM}-{DD} {lugar}")

    def __init__(self, parent, lang, accent, current, geocode):
        super().__init__(parent, lang, accent, tr(lang, "tpl_title"), tr(lang, "tpl_intro"), "tpl_save", None)
        items = [(p.replace("/", " / "), p) for p in self.PRESETS]
        self.cmb = self.combo("tpl_preset", items + [(tr(lang, "tpl_custom"), "")], current if current in self.PRESETS
                              else "")
        self.edit = QLineEdit(current or self.PRESETS[0])
        self.add_row("tpl_template", self.edit)
        self.chk_geo = self.toggle("tpl_geocode", geocode)
        hint = QLabel(tr(lang, "tpl_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.5);")
        self.extra.addWidget(hint)
        self.cmb.currentIndexChanged.connect(self._preset)
        self.edit.textChanged.connect(lambda _t: self._update())
        self._update()

    def _preset(self, _i):
        if self.cmb.currentData():
            self.edit.setText(self.cmb.currentData())

    def _update(self):
        from ..foldertemplate import render_template, template_uses_place
        from datetime import datetime
        tpl = self.template()
        ok = bool(tpl)
        example = render_template(tpl, datetime(2026, 7, 14, 10, 30), "HERO5 Black", "Málaga") if ok else ""
        self.note(tr(self.lang, "tpl_example", ex=example.replace("/", os.sep)) if ok else "")
        self.btn_ok.setEnabled(ok)
        self.chk_geo.setVisible(template_uses_place(tpl))

    def template(self):
        return self.edit.text().strip().strip("/\\")
