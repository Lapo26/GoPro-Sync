"""Plantillas para las carpetas de la sincronizacion y nombre del lugar.

Plantilla: texto con {AAAA} (o {YYYY}), {AA}, {MM}, {DD} y {lugar}; "/"
separa subcarpetas. Ejemplos: "{AAAA}-{MM}-{DD}" (lo de siempre, como Quik),
"{AAAA}/{AAAA}-{MM}-{DD} {lugar}".

El lugar no se conoce al copiar (de la camara no se puede leer el GPS sin
copiar el archivo): se copia en la carpeta sin lugar y, al terminar, las
carpetas del dia que se tocaron se renombran con el lugar, que sale del GPS
de los videos / fotos de ese dia y del servicio gratuito de OpenStreetMap
(Nominatim). Solo se consulta si el usuario lo activo."""
import json
import os
import re
import shutil
import threading
import time

DEFAULT_TEMPLATE = "{AAAA}-{MM}-{DD}"
_BAD = re.compile(r'[<>:"|?*\x00-\x1f\\]')

# carpetas de dia tocadas por la sincronizacion en curso (sin el lugar)
TOUCHED = set()
_TOUCHED_LOCK = threading.Lock()


def template_uses_place(tpl):
    return "{lugar}" in (tpl or "") or "{place}" in (tpl or "")


def _clean(part):
    part = _BAD.sub("", part)
    part = re.sub(r"\s+", " ", part).strip(" .-_")
    return part


def render_template(tpl, dt, camera="", place=""):
    """Ruta relativa (con "/") para la fecha dt."""
    tpl = tpl or DEFAULT_TEMPLATE
    values = {"AAAA": f"{dt.year:04d}", "YYYY": f"{dt.year:04d}", "AA": f"{dt.year % 100:02d}",
              "YY": f"{dt.year % 100:02d}", "MM": f"{dt.month:02d}", "DD": f"{dt.day:02d}",
              "lugar": place or "", "place": place or "", "camara": camera or "", "camera": camera or ""}
    out = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), tpl)
    parts = [_clean(p) for p in out.replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    return "/".join(parts) or f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def date_folder(target_dir, dt, tpl=None):
    """Carpeta de destino para la fecha (sin el lugar) y la anota como tocada."""
    rel = render_template(tpl, dt, place="")
    path = os.path.join(target_dir, *rel.split("/"))
    if template_uses_place(tpl):
        with _TOUCHED_LOCK:
            TOUCHED.add((path, dt.strftime("%Y-%m-%d")))
    return path


def take_touched():
    with _TOUCHED_LOCK:
        out = set(TOUCHED)
        TOUCHED.clear()
    return out


# ---------------------------------------------------------------- lugar
_GEO_CACHE = {"data": None}
_GEO_LOCK = threading.Lock()
_LAST_CALL = [0.0]


def _geo_file():
    from .env import CONFIG_DIR
    return os.path.join(CONFIG_DIR, "geocode_cache.json")


def reverse_geocode(lat, lon, lang="es"):
    """Nombre de la localidad para unas coordenadas (o "" si no se pudo).
    Se guarda en cache por zona (~1 km) y se respeta el limite de
    Nominatim (1 consulta por segundo)."""
    key = f"{lat:.2f}|{lon:.2f}|{lang}"
    with _GEO_LOCK:
        if _GEO_CACHE["data"] is None:
            try:
                with open(_geo_file(), encoding="utf-8") as f:
                    _GEO_CACHE["data"] = json.load(f)
            except Exception:
                _GEO_CACHE["data"] = {}
        if key in _GEO_CACHE["data"]:
            return _GEO_CACHE["data"][key]
        wait = 1.1 - (time.monotonic() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.monotonic()
    name = ""
    try:
        import urllib.request
        from .version import APP_VERSION
        url = (f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&lat={lat:.5f}&lon={lon:.5f}"
               f"&accept-language={lang}")
        req = urllib.request.Request(url, headers={"User-Agent": f"GoProSync/{APP_VERSION} (desktop app)"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
        addr = data.get("address") or {}
        for k in ("city", "town", "village", "municipality", "hamlet", "county", "state_district", "state", "country"):
            if addr.get(k):
                name = addr[k]
                break
    except Exception as e:
        print("[Lugar]", e)
        return ""                       # sin conexion: no se guarda, se reintenta otra vez
    with _GEO_LOCK:
        _GEO_CACHE["data"][key] = name
        try:
            tmp = _geo_file() + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_GEO_CACHE["data"], f, ensure_ascii=False)
            os.replace(tmp, _geo_file())
        except Exception:
            pass
    return name


def folder_location(folder, max_files=6):
    """(lat, lon) de algun video o foto con GPS dentro de la carpeta."""
    from .media import read_gopro_track, read_jpeg_exif
    vids, pics = [], []
    for d, _dirs, names in os.walk(folder):
        for n in sorted(names):
            low = n.lower()
            if low.endswith(".mp4"):
                vids.append(os.path.join(d, n))
            elif low.endswith((".jpg", ".jpeg")):
                pics.append(os.path.join(d, n))
    for p in pics[:max_files]:
        gps = (read_jpeg_exif(p) or {}).get("gps") or {}
        try:
            def deg(v):
                return v[0][0] / v[0][1] + v[1][0] / v[1][1] / 60 + v[2][0] / v[2][1] / 3600
            lat = deg(gps["lat"]) * (-1 if gps.get("latref") == "S" else 1)
            lon = deg(gps["lon"]) * (-1 if gps.get("lonref") == "W" else 1)
            if lat or lon:
                return lat, lon
        except Exception:
            pass
    for p in vids[:max_files]:
        try:
            st = read_gopro_track(p)
        except Exception:
            continue
        if st and st.get("points"):
            q = st["points"][len(st["points"]) // 2]
            return q[0], q[1]
    return None


def _merge_move(src_dir, dst_dir):
    """Mueve el contenido de src_dir dentro de dst_dir (sin pisar nada)."""
    for name in os.listdir(src_dir):
        s = os.path.join(src_dir, name)
        d = os.path.join(dst_dir, name)
        if os.path.isdir(s):
            os.makedirs(d, exist_ok=True)
            _merge_move(s, d)
            try:
                os.rmdir(s)
            except OSError:
                pass
            continue
        if os.path.exists(d):
            if os.path.getsize(d) == os.path.getsize(s):
                continue                      # ya esta: se deja el original donde estaba
            base, ext = os.path.splitext(name)
            k = 1
            while os.path.exists(d):
                d = os.path.join(dst_dir, f"{base}_{k}{ext}")
                k += 1
        shutil.move(s, d)
    try:
        os.rmdir(src_dir)
    except OSError:
        pass


def apply_places(target_dir, touched, tpl, lang="es", log=print):
    """Renombra las carpetas del dia tocadas con el nombre del lugar.
    Devuelve [(antes, despues)]."""
    from datetime import datetime
    done = []
    for path, day in sorted(touched):
        if not os.path.isdir(path):
            continue
        loc = folder_location(path)
        if not loc:
            continue
        place = reverse_geocode(loc[0], loc[1], lang)
        if not place:
            continue
        dt = datetime.strptime(day, "%Y-%m-%d")
        new_rel = render_template(tpl, dt, place=place)
        new_path = os.path.join(target_dir, *new_rel.split("/"))
        if os.path.normcase(new_path) == os.path.normcase(path):
            continue
        try:
            if os.path.exists(new_path):
                _merge_move(path, new_path)
            else:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                os.rename(path, new_path)
            done.append((path, new_path))
        except Exception as e:
            log(f"[Lugar] no se pudo renombrar {path}: {e}")
    return done


def existing_day_folders(target_dir, tpl):
    """Carpetas del dia ya existentes que todavia no tienen lugar."""
    out = set()
    pat = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
    for d, dirs, _names in os.walk(target_dir):
        for n in dirs:
            m = pat.match(n)
            if m:
                out.add((os.path.join(d, n), f"{m.group(1)}-{m.group(2)}-{m.group(3)}"))
        if d.count(os.sep) - target_dir.count(os.sep) >= 2:
            dirs[:] = []
    return out
