"""Control de la camara por Wi-Fi (GoPro HERO5 a HERO7, protocolo clasico
http://10.5.5.9): poner en hora, ver la lista de archivos y borrar la
tarjeta. La PC tiene que estar conectada a la red Wi-Fi de la camara."""
import json
import os
import urllib.request
from datetime import datetime

BASE = "http://10.5.5.9"


def _get(path, timeout=4):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "GoProSync"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def status():
    """Estado de la camara (dict) o None si no esta conectada por Wi-Fi."""
    try:
        return json.loads(_get("/gp/gpControl/status").decode("utf-8", "replace"))
    except Exception:
        return None


def info():
    """Modelo y firmware (si la camara los informa)."""
    try:
        data = json.loads(_get("/gp/gpControl").decode("utf-8", "replace"))
        inf = data.get("info") or {}
        return {"model": inf.get("model_name") or "", "firmware": inf.get("firmware_version") or ""}
    except Exception:
        return {}


def battery_percent(st):
    try:
        s = st.get("status") or {}
        if "70" in s:                         # porcentaje (HERO5+)
            return int(s["70"])
        return {0: 0, 1: 15, 2: 50, 3: 100}.get(int(s.get("2", -1)))
    except Exception:
        return None


def datetime_param(dt):
    """Fecha y hora en el formato de la camara: %AA%MM%DD%hh%mm%ss en hexadecimal."""
    vals = (dt.year % 100, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    return "".join(f"%{v:02x}" for v in vals)


def set_datetime(dt=None):
    dt = dt or datetime.now()
    _get("/gp/gpControl/command/setup/date_time?p=" + datetime_param(dt))
    return dt


def media_list():
    """[(carpeta, nombre, tamano)] de lo que hay en la tarjeta."""
    data = json.loads(_get("/gp/gpMediaList", timeout=15).decode("utf-8", "replace"))
    out = []
    for folder in data.get("media") or []:
        for f in folder.get("fs") or []:
            try:
                out.append((folder.get("d", ""), f["n"], int(f.get("s") or 0)))
            except (KeyError, ValueError):
                pass
    return out


def library_index(root):
    """{nombre en minuscula: {tamanos}} de la biblioteca de la PC."""
    idx = {}
    for d, _dirs, names in os.walk(root):
        for n in names:
            try:
                idx.setdefault(n.lower(), set()).add(os.path.getsize(os.path.join(d, n)))
            except OSError:
                pass
    return idx


def not_copied(media, root, extra_roots=()):
    """Archivos de la camara que no estan (mismo nombre y tamano) en la PC."""
    idx = library_index(root)
    for r in extra_roots:
        for k, v in library_index(r).items():
            idx.setdefault(k, set()).update(v)
    missing = []
    for folder, name, size in media:
        sizes = idx.get(name.lower(), set())
        # el nombre puede tener sufijo _1 si se copio dos veces: se acepta igual el tamano
        if size and size not in sizes:
            base, ext = os.path.splitext(name.lower())
            alt = [s for k, v in idx.items() if k.startswith(base + "_") and k.endswith(ext) for s in v]
            if size not in alt:
                missing.append((folder, name, size))
        elif not size and not sizes:
            missing.append((folder, name, size))
    return missing


def delete_all():
    """Borra TODO lo de la tarjeta de la camara. Solo se llama despues de
    comprobar que todo esta copiado y de que el usuario lo confirme."""
    _get("/gp/gpControl/command/storage/delete/all", timeout=30)
