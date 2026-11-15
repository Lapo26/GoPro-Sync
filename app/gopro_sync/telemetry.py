"""Telemetria de la GoPro: momentos HiLight, acelerometro (saltos), series
para los graficos y exportacion del recorrido a GPX / KML.

Todo se lee de los metadatos del MP4 sin bibliotecas extra, igual que el
resto de media.py: la caja 'moov' (al final del archivo) y las muestras de
la pista de telemetria 'gpmd' (GPMF)."""
import json
import math
import os
import struct
import threading
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from .media import _GPMF_FMT, _gpmd_sample_ranges, _mp4_boxes, _read_moov, read_gopro_track, read_mp4_info


# ---------------------------------------------------------------- HiLight
def _klv_find(buf, pos, end, key, out):
    """Busca la clave 'key' en un bloque GPMF (KLV anidado) y junta sus valores."""
    while pos + 8 <= end:
        k = buf[pos:pos + 4]
        typ = buf[pos + 4]
        rep = struct.unpack(">H", buf[pos + 6:pos + 8])[0]
        dstart, dlen = pos + 8, buf[pos + 5] * rep
        if dstart + dlen > end or not k.strip(b"\x00"):
            break
        if typ == 0:
            _klv_find(buf, dstart, dstart + dlen, key, out)
        elif k == key and chr(typ) in _GPMF_FMT:
            fmt = _GPMF_FMT[chr(typ)]
            n = dlen // struct.calcsize(fmt)
            out.extend(struct.unpack(f">{n}{fmt}", buf[dstart:dstart + n * struct.calcsize(fmt)]))
        pos = dstart + ((dlen + 3) & ~3)


def read_hilights(path):
    """Momentos HiLight (boton o voz "GoPro HiLight") en milisegundos desde
    el comienzo del video, ordenados. HERO5: caja udta/HMMT (cantidad + lista).
    HERO6 y posteriores: dentro del bloque GPMF de udta (HLMT/HMMT)."""
    try:
        moov = _read_moov(path)
    except OSError:
        return []
    if not moov:
        return []
    marks = []
    for typ, a, b in _mp4_boxes(moov, 8):
        if typ != b"udta":
            continue
        for t2, a2, b2 in _mp4_boxes(moov, a, b):
            if t2 == b"HMMT" and b2 - a2 >= 4:
                n = struct.unpack(">I", moov[a2:a2 + 4])[0]
                n = min(n, (b2 - a2 - 4) // 4, 1000)
                marks.extend(struct.unpack(f">{n}I", moov[a2 + 4:a2 + 4 + 4 * n]))
            elif t2 == b"GPMF":
                found = []
                try:
                    _klv_find(moov, a2, b2, b"HMMT", found)
                except Exception:
                    found = []
                marks.extend(found)
    dur = (read_mp4_info(path).get("duration") or 0) * 1000
    out = sorted({int(m) for m in marks if m > 0 and (not dur or m <= dur + 1000)})
    return out


# ---------------------------------------------------------------- cache por archivo
class _JsonCache:
    """Cache en disco {ruta|tamano|fecha: valor} compartida entre hilos."""

    def __init__(self, name):
        self.name = name
        self.data = None
        self.lock = threading.Lock()

    def _file(self):
        from .env import CONFIG_DIR
        return os.path.join(CONFIG_DIR, self.name)

    def get_many(self, paths, compute):
        with self.lock:
            if self.data is None:
                try:
                    with open(self._file(), encoding="utf-8") as fh:
                        self.data = json.load(fh)
                except Exception:
                    self.data = {}
        out, changed = {}, False
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = f"{os.path.normcase(p)}|{st.st_size}|{int(st.st_mtime)}"
            with self.lock:
                val = self.data.get(key)
            if val is None:
                try:
                    val = compute(p)
                except Exception:
                    val = 0
                with self.lock:
                    self.data[key] = val
                changed = True
            out[p] = val
        if changed:
            self.save()
        return out

    def save(self):
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self._file()), exist_ok=True)
                tmp = self._file() + ".part"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh)
                os.replace(tmp, self._file())
            except Exception:
                pass


HILIGHT_CACHE = _JsonCache("hilight_cache.json")
VIDEO_INFO_CACHE = _JsonCache("video_info_cache.json")
TRACK_CACHE = _JsonCache("track_cache.json")


def hilight_counts(paths):
    """{ruta: cantidad de HiLights} (en cache)."""
    return HILIGHT_CACHE.get_many(paths, lambda p: len(read_hilights(p)))


def _video_summary(path):
    """Duracion, resolucion y fps de un video (para filtros y estadisticas)."""
    mi = read_mp4_info(path)
    v = mi.get("video") or {}
    fps = (v["timescale"] / v["delta"]) if v.get("timescale") and v.get("delta") else 0
    return {"d": round(mi.get("duration") or 0, 2), "w": v.get("w") or 0, "h": v.get("h") or 0, "fps": round(fps, 2)}


def video_summaries(paths):
    return VIDEO_INFO_CACHE.get_many(paths, _video_summary)


def _track_summary(path):
    """Recorrido reducido (hasta ~300 puntos) + distancia, para el mapa de viajes."""
    st = read_gopro_track(path)
    if not st or not st.get("points"):
        return {"n": 0}
    pts = st["points"]
    step = max(1, len(pts) // 300)
    small = [[round(p[0], 6), round(p[1], 6)] for p in pts[::step]]
    if small[-1] != [round(pts[-1][0], 6), round(pts[-1][1], 6)]:
        small.append([round(pts[-1][0], 6), round(pts[-1][1], 6)])
    utc = next((u for u in st.get("utcs") or [] if u), None)
    return {"n": len(pts), "pts": small, "dist": round(st.get("dist") or 0, 1), "vmax": round(st.get("vmax") or 0, 1),
            "utc": utc}


def track_summaries(paths):
    return TRACK_CACHE.get_many(paths, _track_summary)


# ---------------------------------------------------------------- acelerometro
def _gpmf_streams(buf, pos, end, keys, out):
    """Junta los valores (ya escalados) de las claves pedidas, por muestra."""
    scal = None
    while pos + 8 <= end:
        key = buf[pos:pos + 4]
        typ = buf[pos + 4]
        ssize = buf[pos + 5]
        rep = struct.unpack(">H", buf[pos + 6:pos + 8])[0]
        dstart, dlen = pos + 8, ssize * rep
        if dstart + dlen > end:
            break
        if typ == 0:
            _gpmf_streams(buf, dstart, dstart + dlen, keys, out)
        elif key == b"SCAL" and chr(typ) in _GPMF_FMT:
            fmt = _GPMF_FMT[chr(typ)]
            n = dlen // struct.calcsize(fmt)
            scal = struct.unpack(f">{n}{fmt}", buf[dstart:dstart + n * struct.calcsize(fmt)])
        elif key in keys and chr(typ) in _GPMF_FMT:
            fmt = _GPMF_FMT[chr(typ)]
            width = struct.calcsize(fmt)
            per = max(1, ssize // width)               # valores por muestra (3 ejes)
            n = (dlen // width) // per * per
            vals = struct.unpack(f">{n}{fmt}", buf[dstart:dstart + n * width])
            sc = list(scal) if scal else [1]
            if len(sc) < per:
                sc = sc + [sc[-1]] * (per - len(sc))
            rows = [tuple(vals[i + k] / (sc[k] or 1) for k in range(per)) for i in range(0, n, per)]
            out.setdefault(key, []).extend(rows)
        pos = dstart + ((dlen + 3) & ~3)


def read_accel(path):
    """(tiempos en s, magnitud de la aceleracion en m/s^2) del acelerometro.
    Las muestras de cada bloque se reparten en el tiempo que dura el bloque."""
    moov = _read_moov(path)
    if not moov:
        return [], []
    ranges = _gpmd_sample_ranges(moov)
    if not ranges:
        return [], []
    dur = read_mp4_info(path).get("duration") or len(ranges)
    per_block = dur / len(ranges)
    times, mags = [], []
    with open(path, "rb") as f:
        for bi, (off, size) in enumerate(ranges):
            if size <= 0 or size > 4 * 1024 * 1024:
                continue
            f.seek(off)
            buf = f.read(size)
            out = {}
            try:
                _gpmf_streams(buf, 0, len(buf), (b"ACCL",), out)
            except Exception:
                continue
            rows = out.get(b"ACCL") or []
            n = len(rows)
            for j, r in enumerate(rows):
                if len(r) >= 3:
                    times.append((bi + j / n) * per_block)
                    mags.append(math.sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]))
    return times, mags


G = 9.80665


def detect_jumps(times, mags, min_air=0.3, threshold_g=0.35):
    """Saltos: tramos en caida libre (aceleracion casi 0 g) de al menos
    min_air segundos. Devuelve [{"t", "air", "g"}] con el inicio, el tiempo
    en el aire y el golpe al aterrizar (en g)."""
    if len(mags) < 10:
        return []
    # promedio movil corto: el ruido de la vibracion no corta un salto
    win = 5
    smooth, acc = [], 0.0
    for i, m in enumerate(mags):
        acc += m
        if i >= win:
            acc -= mags[i - win]
        smooth.append(acc / min(i + 1, win))
    out, start = [], None
    limit = threshold_g * G
    for i, m in enumerate(smooth):
        if m < limit:
            if start is None:
                start = i
        elif start is not None:
            air = times[i] - times[start]
            if air >= min_air:
                end_t = times[i] + 0.6
                peak, j = 0.0, i
                while j < len(mags) and times[j] <= end_t:
                    peak = max(peak, mags[j])
                    j += 1
                out.append({"t": round(times[start], 2), "air": round(air, 2), "g": round(peak / G, 1)})
            start = None
    # descartar "saltos" absurdos (la camara cayendo de una mochila, etc.): mas de 4 s
    return [j for j in out if j["air"] <= 4.0]


def read_jumps(path):
    try:
        t, m = read_accel(path)
    except Exception:
        return []
    return detect_jumps(t, m)


# ---------------------------------------------------------------- series para el grafico
def chart_series(track, duration, max_points=1200):
    """[(segundo del video, km/h, altura m)] a partir del recorrido limpio."""
    if not track or not track.get("points"):
        return []
    pts, fracs = track["points"], track.get("fracs") or []
    n = len(pts)
    if len(fracs) != n:
        fracs = [i / max(1, n - 1) for i in range(n)]
    step = max(1, n // max_points)
    out = []
    for i in range(0, n, step):
        lo, hi = max(0, i - step), min(n, i + step + 1)
        v = sum(p[3] for p in pts[lo:hi]) / (hi - lo) * 3.6
        out.append((fracs[i] * duration, v, pts[i][2]))
    return out


# ---------------------------------------------------------------- GPX / KML
def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _point_times(track, path):
    """Hora UTC de cada punto: la del GPS (GPSU) o, si no hay, la de creacion
    del video + el avance dentro del video."""
    utcs = track.get("utcs") or []
    if utcs and all(u for u in utcs):
        return utcs
    from .media import _read_mp4_creation_time
    start = _read_mp4_creation_time(path)
    base = start.astimezone(timezone.utc).timestamp() if start else os.path.getmtime(path)
    dur = track.get("elapsed") or read_mp4_info(path).get("duration") or 0
    fr = track.get("fracs") or [i / max(1, len(track["points"]) - 1) for i in range(len(track["points"]))]
    known = [u for u in utcs if u]
    out = []
    for i, f in enumerate(fr):
        u = utcs[i] if i < len(utcs) else None
        out.append(u if u else (known[0] + (f - fr[0]) * dur if known else base + f * dur))
    return out


def export_gpx(path, out_file, track=None):
    """Escribe el recorrido del video como GPX 1.1 (Strava, Garmin, Google Earth...)."""
    track = track or read_gopro_track(path)
    if not track or not track.get("points"):
        return False
    times = _point_times(track, path)
    name = escape(os.path.splitext(os.path.basename(path))[0])
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="GoPro Sync" xmlns="http://www.topografix.com/GPX/1/1"'
             ' xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v2">',
             f"  <metadata><name>{name}</name><time>{_iso(times[0])}</time></metadata>",
             f"  <trk><name>{name}</name><type>GoPro</type><trkseg>"]
    last_t = None
    for p, t in zip(track["points"], times):
        if last_t is not None and t <= last_t:
            continue                  # GPX pide horas crecientes (el GPS manda ~18 puntos por segundo)
        last_t = t
        lines.append(f'    <trkpt lat="{p[0]:.7f}" lon="{p[1]:.7f}"><ele>{p[2]:.1f}</ele><time>{_iso(t)}</time>'
                     f"<extensions><gpxtpx:TrackPointExtension><gpxtpx:speed>{p[3]:.2f}</gpxtpx:speed>"
                     "</gpxtpx:TrackPointExtension></extensions></trkpt>")
    lines += ["  </trkseg></trk>", "</gpx>", ""]
    _write_text(out_file, "\n".join(lines))
    return True


def export_kml(path, out_file, track=None, labels=("Inicio", "Fin")):
    """Escribe el recorrido como KML (Google Earth / Google My Maps)."""
    track = track or read_gopro_track(path)
    if not track or not track.get("points"):
        return False
    name = escape(os.path.splitext(os.path.basename(path))[0])
    pts = track["points"]
    step = max(1, len(pts) // 20000)
    coords = " ".join(f"{p[1]:.7f},{p[0]:.7f},{p[2]:.1f}" for p in pts[::step])
    first, last = pts[0], pts[-1]
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{name}</name>
    <Style id="ruta"><LineStyle><color>ff00d4ff</color><width>4</width></LineStyle></Style>
    <Placemark><name>{name}</name><styleUrl>#ruta</styleUrl>
      <LineString><tessellate>1</tessellate><altitudeMode>clampToGround</altitudeMode>
        <coordinates>{coords}</coordinates></LineString></Placemark>
    <Placemark><name>{escape(labels[0])}</name><Point><coordinates>{first[1]:.7f},{first[0]:.7f},{first[2]:.1f}</coordinates></Point></Placemark>
    <Placemark><name>{escape(labels[1])}</name><Point><coordinates>{last[1]:.7f},{last[0]:.7f},{last[2]:.1f}</coordinates></Point></Placemark>
  </Document>
</kml>
"""
    _write_text(out_file, kml)
    return True


def _write_text(out_file, text):
    tmp = out_file + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, out_file)
