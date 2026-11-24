"""Filtro por fechas comun (galerias y mapa de recorridos): menu con todas
las fechas / hoy / 7 dias / 30 dias / este año / cada año / rango propio."""
from datetime import datetime, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QDateEdit, QDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout

from ..i18n import tr
from ..ui.theme import current_accent, dialog_css


def show_date_menu(parent, button, lang, years, current, on_set):
    """on_set(rango (desde, hasta) 'YYYY-MM-DD' o None, clave de texto o None, etiqueta o None)."""
    today = datetime.now().date()
    fmt = lambda d: d.strftime("%Y-%m-%d")
    menu = QMenu(parent)
    menu.addAction(tr(lang, "date_all"), lambda: on_set(None, "date_all", None))
    menu.addAction(tr(lang, "date_today"), lambda: on_set((fmt(today), fmt(today)), "date_today", None))
    menu.addAction(tr(lang, "date_7"), lambda: on_set((fmt(today - timedelta(days=6)), fmt(today)), "date_7", None))
    menu.addAction(tr(lang, "date_30"), lambda: on_set((fmt(today - timedelta(days=29)), fmt(today)), "date_30", None))
    menu.addAction(tr(lang, "date_this_year"),
                   lambda: on_set((f"{today.year}-01-01", f"{today.year}-12-31"), "date_this_year", None))
    if years:
        menu.addSeparator()
        for y in years:
            y = str(y)
            menu.addAction(y, lambda y=y: on_set((f"{y}-01-01", f"{y}-12-31"), None, y))
    menu.addSeparator()

    def custom():
        res = pick_date_range(parent, lang, current)
        if res is not None:
            on_set(res[0], None, res[1])
    menu.addAction(tr(lang, "date_custom"), custom)
    menu.exec(button.mapToGlobal(button.rect().bottomLeft()))


def pick_date_range(parent, lang, current):
    """Dialogo desde / hasta. Devuelve ((desde, hasta), etiqueta) o None."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr(lang, "date_range_title"))
    dlg.setStyleSheet(dialog_css(current_accent(parent)))
    lay = QVBoxLayout(dlg)
    row = QHBoxLayout()
    start = current[0] if current else datetime.now().strftime("%Y-01-01")
    end = current[1] if current else datetime.now().strftime("%Y-%m-%d")
    d_from = QDateEdit(QDate.fromString(start, "yyyy-MM-dd"))
    d_to = QDateEdit(QDate.fromString(end, "yyyy-MM-dd"))
    for w in (d_from, d_to):
        w.setCalendarPopup(True)
        w.setDisplayFormat("dd/MM/yyyy")
    row.addWidget(QLabel(tr(lang, "date_from")))
    row.addWidget(d_from)
    row.addWidget(QLabel(tr(lang, "date_to")))
    row.addWidget(d_to)
    lay.addLayout(row)
    ok = QPushButton(tr(lang, "date_apply"))
    ok.setObjectName("primary")
    ok.clicked.connect(dlg.accept)
    lay.addWidget(ok, alignment=Qt.AlignmentFlag.AlignRight)
    if not dlg.exec():
        return None
    a, b = d_from.date(), d_to.date()
    if a > b:
        a, b = b, a
    label = f"{a.toString('dd/MM/yyyy')} – {b.toString('dd/MM/yyyy')}"
    return (a.toString("yyyy-MM-dd"), b.toString("yyyy-MM-dd")), label


def date_button_text(lang, key, label):
    return tr(lang, "date_filter_btn", label=label if label is not None else (tr(lang, key) if key else ""))
