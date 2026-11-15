"""Secuencias de fotos de la GoPro: time-lapse y rafaga.

La camara guarda cada foto de una secuencia como Gxxxyyyy.JPG: 'G', el numero
de grupo (3 cifras) y el numero de foto dentro del grupo (4 cifras), p. ej.
G0120001.JPG, G0120002.JPG... Las fotos sueltas son GOPRxxxx.JPG, asi que el
nombre alcanza para agrupar sin leer nada del disco.

Rafaga o time-lapse se distingue por el tiempo entre fotos: una rafaga son
como mucho 30 fotos en unos pocos segundos (30 en 6 s es la mas lenta de la
HERO5); un time-lapse tiene un intervalo fijo de 0,5 s o mas. Si la carpeta
la creo Quik ("Burst Sequence 1", "Time Lapse 1") se usa eso directamente."""
import os
import re

# G0120001.JPG; tambien G0120001_1.JPG (copia renombrada por choque de nombres)
SEQ_NAME_RE = re.compile(r"^G(\d{3})(\d{4})(?:_\d+)?\.(JPG|JPEG|GPR)$", re.IGNORECASE)

BURST_MAX_FRAMES = 30
BURST_MAX_SPAN_S = 7.0        # 30 fotos en 6 s + la resolucion de 1-2 s de la fecha

KIND_TIMELAPSE = "timelapse"
KIND_BURST = "burst"


def parse_seq_name(name):
    """(grupo, foto) si el nombre es de una secuencia, o None."""
    m = SEQ_NAME_RE.match(name)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def classify(n, span_s, folder_name=""):
    low = (folder_name or "").lower()
    if low.startswith("burst"):
        return KIND_BURST
    if low.startswith("time lapse") or low.startswith("timelapse") or low.startswith("night lapse"):
        return KIND_TIMELAPSE
    if n <= BURST_MAX_FRAMES and span_s <= BURST_MAX_SPAN_S:
        return KIND_BURST
    return KIND_TIMELAPSE


def find_sequences(paths, mtimes=None, min_frames=2):
    """Agrupa las fotos de secuencia. 'mtimes' es ruta -> fecha (del listado
    de la carpeta, sin tocar el disco). Devuelve una lista de dicts:
      {"key", "group", "dir", "kind", "frames": [rutas en orden], "span": s}
    Solo las fotos JPG/JPEG cuentan como cuadros (el .GPR es el RAW de la misma foto)."""
    mtimes = mtimes or {}
    buckets = {}
    for p in paths:
        name = os.path.basename(p)
        parsed = parse_seq_name(name)
        if not parsed or name.lower().endswith(".gpr"):
            continue
        group, frame = parsed
        d = os.path.dirname(p)
        buckets.setdefault((os.path.normcase(d), group), []).append((frame, name.lower(), p))
    out = []
    for (nd, group), items in buckets.items():
        if len(items) < min_frames:
            continue
        items.sort()
        frames, seen = [], set()
        for frame, _n, p in items:
            if frame in seen:              # G0120001.JPG y G0120001_1.JPG: la misma foto
                continue
            seen.add(frame)
            frames.append(p)
        if len(frames) < min_frames:
            continue
        times = [mtimes.get(p) for p in frames]
        times = [t for t in times if t]
        span = (max(times) - min(times)) if len(times) >= 2 else 0.0
        d = os.path.dirname(frames[0])
        kind = classify(len(frames), span, os.path.basename(d))
        out.append({"key": f"{nd}|{group}", "group": group, "dir": d, "kind": kind,
                    "frames": frames, "span": span})
    return out


def sequence_of(path):
    """La secuencia a la que pertenece una foto (leyendo su carpeta), o None."""
    parsed = parse_seq_name(os.path.basename(path))
    if not parsed:
        return None
    d = os.path.dirname(path)
    group = parsed[0]
    paths, mtimes = [], {}
    try:
        with os.scandir(d) as it:
            for e in it:
                p = parse_seq_name(e.name)
                if p and p[0] == group:
                    paths.append(e.path)
                    try:
                        mtimes[e.path] = e.stat().st_mtime
                    except OSError:
                        pass
    except OSError:
        return None
    seqs = find_sequences(paths, mtimes)
    return seqs[0] if seqs else None


def plan_sequence_folders(items):
    """Para la sincronizacion: items = [(nombre, fecha_epoch o None)] de los
    archivos a copiar. Devuelve nombre_en_minusculas -> subcarpeta
    ("Time Lapse G012" / "Burst G012") para cada foto de una secuencia, con
    el mismo nombre que usaba Quik. El .GPR va con su JPG."""
    groups = {}
    for name, ts in items:
        parsed = parse_seq_name(name)
        if parsed:
            groups.setdefault(parsed[0], []).append((name, ts))
    out = {}
    for group, members in groups.items():
        jpgs = [(n, t) for n, t in members if not n.lower().endswith(".gpr")]
        if len(jpgs) < 2:
            continue
        times = [t for _n, t in jpgs if t]
        span = (max(times) - min(times)) if len(times) >= 2 else 0.0
        kind = classify(len(jpgs), span)
        folder = f"{'Burst' if kind == KIND_BURST else 'Time Lapse'} G{group}"
        for n, _t in members:
            out[n.lower()] = folder
    return out
