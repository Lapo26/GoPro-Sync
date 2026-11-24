"""Estilo comun de todas las ventanas: colores, botones y dialogos.

Antes cada dialogo copiaba su propia hoja de estilo (con pequeñas
diferencias de padding, colores de lista y desplegables con la flecha de
Windows). Ahora todos salen de aca."""
import os

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF

from ..env import CACHE_DIR, FONT_STACK, load_config

DEFAULT_ACCENT = "#00f2fe"
BG = "#1c1e29"          # fondo de dialogos
POPUP = "#1f2230"       # menus, listas desplegables, tooltips
HEADER = "#262a3a"      # encabezados de tablas / calendario
BAR = "#15151c"         # barra de controles de los visores (reproductor, rafagas)
DANGER = "#e5484d"
DANGER_HOVER = "#f2555a"
INK = "#0d0d12"         # texto sobre el color de acento


def rgba(color, alpha):
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def lighter(color, factor=118):
    return QColor(color).lighter(factor).name()


def current_accent(widget=None):
    """Color de acento elegido en Ajustes (desde la ventana principal si se
    llega, si no desde la configuracion guardada)."""
    w = widget
    while w is not None:
        f = getattr(w, "get_current_theme_color", None)
        if callable(f):
            try:
                return f()
            except Exception:
                break
        w = w.parent() if hasattr(w, "parent") else None
    try:
        return load_config().get("accent_color", DEFAULT_ACCENT) or DEFAULT_ACCENT
    except Exception:
        return DEFAULT_ACCENT


def dialog_css(accent, extra=""):
    """Hoja de estilo base de todos los dialogos (titulo, textos, botones
    normales / principal / peligroso, barra de progreso)."""
    return f"""
        QDialog {{ background: {BG}; }}
        QLabel {{ color: #fff; background: transparent; }}
        * {{ font-family: {FONT_STACK}; }}
        QPushButton {{ background: rgba(255,255,255,0.08); color: #fff; border: 1px solid rgba(255,255,255,0.14);
                      border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
        QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
        QPushButton:disabled {{ color: rgba(255,255,255,0.35); }}
        QPushButton#primary {{ background: {accent}; color: {INK}; border: none; font-weight: 800; }}
        QPushButton#primary:hover {{ background: {lighter(accent)}; }}
        QPushButton#primary:disabled {{ background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.4); }}
        QPushButton#danger {{ background: {DANGER}; color: #fff; border: none; font-weight: 800; }}
        QPushButton#danger:hover {{ background: {DANGER_HOVER}; }}
        QProgressBar {{ background: rgba(255,255,255,0.08); border: none; border-radius: 8px; min-height: 10px;
                       text-align: center; color: #fff; font-weight: 700; }}
        QProgressBar::chunk {{ background: {accent}; border-radius: 8px; }}
        QScrollArea {{ border: none; background: transparent; }}
    """ + extra


TITLE_CSS = "font-size: 19px; font-weight: 800;"
INTRO_CSS = "font-size: 12px; color: rgba(255,255,255,0.65);"


def round_button_css(accent, size=38, bg="rgba(255,255,255,0.1)"):
    """Botones redondos con icono de los visores (reproductor, rafagas, fotos)."""
    r = size // 2
    return (f"QPushButton {{ background: {bg}; border-radius: {r}px; border: none; }}"
            " QPushButton:hover { background: rgba(255,255,255,0.2); }"
            f" QPushButton:checked {{ background: {rgba(accent, 0.35)}; }}"
            f" QPushButton:checked:hover {{ background: {rgba(accent, 0.5)}; }}")


_ARROWS = None


def arrow_dir():
    """Carpeta con las flechitas de los desplegables y campos numericos
    (PNG generados una vez; @2x para pantallas con escala)."""
    global _ARROWS
    if _ARROWS is not None:
        return _ARROWS
    d = os.path.join(CACHE_DIR, "ui")
    try:
        os.makedirs(d, exist_ok=True)
        for name, up in (("down", False), ("up", True)):
            for scale, suffix in ((1, ""), (2, "@2x")):
                path = os.path.join(d, f"arrow_{name}_v1{suffix}.png")
                if os.path.exists(path):
                    continue
                s = 12 * scale
                img = QImage(s, s, QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(Qt.GlobalColor.transparent)
                p = QPainter(img)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 200))
                a, b = 0.18 * s, 0.82 * s
                top, bottom = 0.34 * s, 0.68 * s
                pts = ([QPointF(a, bottom), QPointF(b, bottom), QPointF(s / 2, top)] if up else
                       [QPointF(a, top), QPointF(b, top), QPointF(s / 2, bottom)])
                p.drawPolygon(QPolygonF(pts))
                p.end()
                img.save(path, "PNG")
        _ARROWS = d.replace("\\", "/")
    except Exception:
        _ARROWS = ""
    return _ARROWS


def inputs_css(accent):
    """Desplegables y campos numericos iguales en toda la app (antes cada
    ventana tenia el suyo, con la flecha gris de Windows)."""
    d = arrow_dir()
    down = f"image: url({d}/arrow_down_v1.png);" if d else ""
    up = f"image: url({d}/arrow_up_v1.png);" if d else ""
    soft = rgba(accent, 0.25)
    return f"""
QComboBox {{
    background: rgba(255,255,255,0.07); color: #ffffff; border: 1px solid rgba(255,255,255,0.14);
    border-radius: 8px; padding: 5px 28px 5px 10px; min-height: 18px;
}}
QComboBox:hover {{ border: 1px solid rgba(255,255,255,0.28); }}
QComboBox:on, QComboBox:focus {{ border: 1px solid {accent}; }}
QComboBox:disabled {{ color: rgba(255,255,255,0.4); }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px;
    border: none; background: transparent; }}
QComboBox::down-arrow {{ {down} width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {POPUP}; color: #ffffff; border: 1px solid rgba(255,255,255,0.14); padding: 4px;
    outline: none; selection-background-color: {soft}; selection-color: #ffffff;
}}
QAbstractSpinBox {{
    background: rgba(255,255,255,0.07); color: #ffffff; border: 1px solid rgba(255,255,255,0.14);
    border-radius: 8px; padding: 5px 24px 5px 10px; min-height: 18px; selection-background-color: {soft};
}}
QAbstractSpinBox:hover {{ border: 1px solid rgba(255,255,255,0.28); }}
QAbstractSpinBox:focus {{ border: 1px solid {accent}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border; width: 22px; border: none; background: transparent;
}}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; margin-top: 2px; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 2px; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: rgba(255,255,255,0.10);
    border-radius: 5px; }}
QAbstractSpinBox::up-arrow {{ {up} width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ {down} width: 10px; height: 10px; }}
QListWidget::indicator, QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px; border: 2px solid rgba(255,255,255,0.45); background: transparent;
}}
QListWidget::indicator:checked, QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QCheckBox {{ color: #ffffff; spacing: 8px; }}
"""
