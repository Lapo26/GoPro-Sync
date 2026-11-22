"""Tamaño y posición de las ventanas del reproductor, del visor de fotos y del
mapa aparte: se recuerdan (también si quedaron maximizadas) y en Ajustes se
elige con qué tamaño abren el reproductor y el visor."""
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication

# Ajustes > Sistema > "Tamaño del reproductor y del visor de fotos"
VIEWER_SIZE_MODES = ("remember", "small", "medium", "large", "maximized")
_FRACTION = {"small": 0.55, "medium": 0.7, "large": 0.85}


def enable_min_max_buttons(win):
    """Botones de minimizar y maximizar (un QDialog no los trae)."""
    win.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
    win.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
    win.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)


def _screen_for(win):
    parent = win.parentWidget()
    screen = parent.window().screen() if parent is not None else None
    return screen or QGuiApplication.primaryScreen()


def _centered(avail, fraction):
    w, h = int(avail.width() * fraction), int(avail.height() * fraction)
    return QRect(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2, w, h)


def _on_some_screen(rect):
    """La ventana recordada todavia entra en algun monitor (si se desconecto
    el segundo monitor no tiene que abrir fuera de la vista)."""
    for s in QGuiApplication.screens():
        inter = s.availableGeometry().intersected(rect)
        if inter.width() >= 200 and inter.height() >= 150:
            return s.availableGeometry()
    return None


def restore_window(win, key, cfg, mode="remember", default_fraction=0.7):
    """Aplica el tamaño antes de mostrar la ventana (también antes de exec())."""
    avail = _screen_for(win).availableGeometry()
    maximized = mode == "maximized"
    rect = None
    if mode in _FRACTION:
        rect = _centered(avail, _FRACTION[mode])
    elif mode == "remember":
        st = cfg.get(f"window_{key}")
        if isinstance(st, dict):
            try:
                r = QRect(int(st["x"]), int(st["y"]), int(st["w"]), int(st["h"]))
                screen_avail = _on_some_screen(r)
                if screen_avail is not None:
                    rect = QRect(r.topLeft(), r.size().boundedTo(screen_avail.size()))
                maximized = bool(st.get("max"))
            except (KeyError, TypeError, ValueError):
                rect = None
    if rect is None:
        rect = _centered(avail, default_fraction)
    win.setGeometry(rect)
    if maximized:
        win.setWindowState(win.windowState() | Qt.WindowState.WindowMaximized)


def remember_window(win, key, cfg):
    """Guarda en cfg cómo quedó la ventana (tamaño normal + si estaba
    maximizada). Devuelve True si cambió algo."""
    if win.isFullScreen():
        # en pantalla completa se guarda lo de antes (al volver se restaura eso)
        maximized = bool(win.property("gpsync_was_maximized"))
    else:
        maximized = win.isMaximized()
    g = win.normalGeometry() if (maximized or win.isFullScreen()) else win.geometry()
    if not g.isValid() or g.width() < 100:
        g = win.geometry()
    st = {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(), "max": maximized}
    if cfg.get(f"window_{key}") == st:
        return False
    cfg[f"window_{key}"] = st
    return True
