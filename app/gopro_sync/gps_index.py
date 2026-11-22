"""Indice GPS de toda la biblioteca: el mapa global y el mapa del reproductor.

Leer el GPS de un video lleva segundos (se recorre toda la telemetria en
Python), asi que no se puede hacer cada vez:
- Se guarda en una base SQLite (cache del usuario) un resumen por archivo:
  punto de inicio, limites, distancia, duracion, fecha, el recorrido
  simplificado para el mapa global (Douglas-Peucker, unos cientos de puntos)
  y un recorrido liviano con telemetria (posicion, altura, velocidad y
  momento del video, hasta ~2 por segundo) para el mapa y el grafico del
  reproductor, que asi abren al instante. Clave: ruta + tamano + fecha de
  modificacion; lo ya leido no se vuelve a leer, y los archivos sin GPS
  tambien quedan anotados.
- La lectura masiva la hacen procesos aparte de la misma app
  (``--gps-index``), con prioridad baja: no compiten por el GIL con la
  ventana, que solo consulta la base cada tanto. Se guarda archivo por
  archivo: si se corta, la proxima vez sigue con lo que falta.
- Un video que se abre en el reproductor antes de estar indexado se lee como
  siempre y queda guardado en el indice en ese momento.

(La exportacion a GPX/KML y el video con datos encima siguen leyendo el GPS
completo: el recorrido del indice es mas liviano.)

Este modulo no usa la interfaz (lo importa el proceso de fondo)."""
import array
import json
import math
import os
import sqlite3
import sys
import threading
import time
import zlib

SCHEMA_VERSION = 2
VIDEO_EXTS = (".mp4",)
PHOTO_EXTS = (".jpg", ".jpeg")
SIMPLIFY_M = 3.0             # tolerancia de la simplificacion del mapa global (metros)
MAX_ROUTE_PTS = 4000         # tope de puntos del recorrido del mapa global
MAX_TRACK_PTS = 7200         # tope del recorrido del reproductor (2 por segundo en 1 hora)
_SKIP_DIRS = {"$recycle.bin", "system volume information", "thumb_cache", "__pycache__"}
IN_WORKER = False


def default_db_path():
    from .env import CACHE_DIR
    return os.path.join(CACHE_DIR, "gps_index.sqlite")


def connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")       # la ventana lee mientras los procesos escriben
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    ver = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    if ver is None or int(ver[0]) != SCHEMA_VERSION:
        conn.execute("DROP TABLE IF EXISTS media")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media (
            key TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime INTEGER, kind TEXT, ok INTEGER,
            t REAL, lat REAL, lon REAL, minlat REAL, maxlat REAL, minlon REAL, maxlon REAL,
            dist REAL, dur REAL, npts INTEGER, route BLOB, track BLOB, stats TEXT, seq INTEGER);
        CREATE INDEX IF NOT EXISTS media_seq ON media(seq);
    """)
    if ver is None or int(ver[0]) != SCHEMA_VERSION:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def get_meta(conn):
    return dict(conn.execute("SELECT k, v FROM meta").fetchall())


def _set_meta(conn, **kv):
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in kv.items()])


# ---------------------------------------------------------------- recorridos
def simplify(points, tol_m=SIMPLIFY_M):
    """Douglas-Peucker (iterativo) sobre [(lat, lon), ...] en metros
    aproximados: deja los puntos que hacen falta para que la linea no se
    aparte mas de tol_m de la original."""
    n = len(points)
    if n <= 2:
        return list(points)
    sample = points[:: max(1, n // 64)]
    lat0 = math.radians(sum(p[0] for p in sample) / len(sample))
    kx, ky = 111320.0 * math.cos(lat0), 110540.0
    xs = [p[1] * kx for p in points]
    ys = [p[0] * ky for p in points]
    keep = bytearray(n)
    keep[0] = keep[-1] = 1
    tol2 = tol_m * tol_m
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = xs[a], ys[a]
        dx, dy = xs[b] - ax, ys[b] - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xs[i] - ax, ys[i] - ay
            if L2 > 0:
                cr = px * dy - py * dx
                d2 = cr * cr / L2
            else:
                d2 = px * px + py * py
            if d2 > best:
                best, bi = d2, i
        if best > tol2:
            keep[bi] = 1
            stack.append((a, bi))
            stack.append((bi, b))
    return [points[i] for i in range(n) if keep[i]]


def _pack(arr):
    if sys.byteorder != "little":
        arr.byteswap()
    return zlib.compress(arr.tobytes(), 6)


def _unpack(blob):
    arr = array.array("i")
    if blob:
        arr.frombytes(zlib.decompress(blob))
        if sys.byteorder != "little":
            arr.byteswap()
    return arr


def encode_route(points):
    arr = array.array("i")
    for p in points:
        arr.append(int(round(p[0] * 1e6)))
        arr.append(int(round(p[1] * 1e6)))
    return _pack(arr)


def decode_route(blob):
    """array('i') con lat, lon intercalados en millonesimas de grado."""
    return _unpack(blob)


def _light_track(track):
    """Recorrido del reproductor: como mucho MAX_TRACK_PTS puntos; cada uno
    con la velocidad y altura promedio de su tramo (no se pierden los picos
    de un punto suelto ni queda ruido)."""
    pts = track["points"]
    fracs = track.get("fracs") or []
    n = len(pts)
    if len(fracs) != n:
        fracs = [i / max(1, n - 1) for i in range(n)]
    step = max(1, -(-n // MAX_TRACK_PTS))
    arr = array.array("i")
    for i in range(0, n, step):
        seg = pts[i:i + step]
        mid = seg[len(seg) // 2]
        alt = sum(p[2] for p in seg) / len(seg)
        spd = sum(p[3] for p in seg) / len(seg)
        arr.extend((int(round(mid[0] * 1e6)), int(round(mid[1] * 1e6)), int(round(alt * 10)),
                    int(round(spd * 100)), int(round(fracs[min(n - 1, i + len(seg) // 2)] * 1e6))))
    return _pack(arr)


def _track_from_row(blob, stats):
    """El dict de read_gopro_track (lo que usan el mapa y el grafico) a
    partir de lo guardado en el indice."""
    from .media import cumulative_distance
    st = json.loads(stats) if stats else {}
    if not blob:
        return {"points": [], "nofix": True} if st.get("nofix") else None
    arr = _unpack(blob)
    pts, fracs = [], []
    for i in range(0, len(arr) - 4, 5):
        pts.append((arr[i] / 1e6, arr[i + 1] / 1e6, arr[i + 2] / 10.0, arr[i + 3] / 100.0))
        fracs.append(arr[i + 4] / 1e6)
    track = dict(st)
    track.update(points=pts, fracs=fracs, cum=cumulative_distance(pts), from_index=True)
    return track


# ---------------------------------------------------------------- lectura
def _exif_deg(vals, ref):
    try:
        d, m, s = (a / b if b else 0.0 for a, b in vals)
        v = d + m / 60.0 + s / 3600.0
        return -v if str(ref).upper().startswith(("S", "W")) else v
    except Exception:
        return None


def _capture_time(path):
    try:
        from .media import get_media_capture_date
        return get_media_capture_date(path).timestamp()
    except Exception:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None


def _empty_row(kind):
    return {"kind": kind, "ok": 0, "t": None, "lat": None, "lon": None, "minlat": None, "maxlat": None,
            "minlon": None, "maxlon": None, "dist": None, "dur": None, "npts": 0, "route": None,
            "track": None, "stats": None}


def index_file(path, kind, track=None):
    """Resumen GPS de un archivo (dict con las columnas de la tabla). Con
    track (un video ya leido por el reproductor) no se vuelve a leer."""
    row = _empty_row(kind)
    row["t"] = _capture_time(path)
    if kind == "p":
        from .media import read_jpeg_exif
        gps = read_jpeg_exif(path).get("gps") or {}
        if "lat" in gps and "lon" in gps:
            lat = _exif_deg(gps["lat"], gps.get("latref", "N"))
            lon = _exif_deg(gps["lon"], gps.get("lonref", "E"))
            if lat is not None and lon is not None and (abs(lat) > 1e-6 or abs(lon) > 1e-6) \
                    and -90 <= lat <= 90 and -180 <= lon <= 180:
                row.update(ok=1, lat=lat, lon=lon, minlat=lat, maxlat=lat, minlon=lon, maxlon=lon)
        return row
    if track is None:
        from .media import read_gopro_track
        track = read_gopro_track(path)
    pts = (track or {}).get("points") or []
    if not pts:
        row["stats"] = json.dumps({"nofix": bool(track and track.get("nofix"))})
        return row
    ll = [(p[0], p[1]) for p in pts]
    simple = simplify(ll)
    tol = SIMPLIFY_M
    while len(simple) > MAX_ROUTE_PTS:
        tol *= 2
        simple = simplify(ll, tol)
    lats = [p[0] for p in ll]
    lons = [p[1] for p in ll]
    dur = track.get("elapsed")
    if not dur:
        from .media import read_mp4_info
        dur = read_mp4_info(path).get("duration") or None
    stats = {k: track.get(k) for k in ("dist", "vmax", "vavg", "amin", "amax", "elapsed")}
    row.update(ok=1, lat=ll[0][0], lon=ll[0][1], minlat=min(lats), maxlat=max(lats),
               minlon=min(lons), maxlon=max(lons), dist=track.get("dist") or 0.0, dur=dur,
               npts=len(simple), route=encode_route(simple), track=_light_track(track),
               stats=json.dumps(stats))
    return row


def path_key(path):
    """Clave de un archivo en el indice: la misma ruta escrita distinto
    (barras, mayusculas en Windows) es el mismo archivo."""
    return os.path.normcase(os.path.abspath(path))


def _insert(conn, path, size, mtime, row):
    conn.execute(
        "INSERT OR REPLACE INTO media (key, path, size, mtime, kind, ok, t, lat, lon, minlat, maxlat, minlon, maxlon,"
        " dist, dur, npts, route, track, stats, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        " (SELECT IFNULL(MAX(seq), 0) + 1 FROM media))",
        (path_key(path), path, size, mtime, row["kind"], row["ok"], row["t"], row["lat"], row["lon"], row["minlat"], row["maxlat"],
         row["minlon"], row["maxlon"], row["dist"], row["dur"], row["npts"], row["route"], row["track"],
         row["stats"]))


def walk_library(roots):
    """[(ruta, tamano, mtime, tipo)] de videos y fotos debajo de las carpetas."""
    out, seen = [], set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            name = e.name
                            if e.is_dir(follow_symlinks=False):
                                if not name.startswith(".") and name.lower() not in _SKIP_DIRS:
                                    stack.append(e.path)
                                continue
                            low = name.lower()
                            kind = "v" if low.endswith(VIDEO_EXTS) else "p" if low.endswith(PHOTO_EXTS) else None
                            if kind is None or low.startswith("._"):
                                continue
                            key = path_key(e.path)
                            if key in seen:
                                continue
                            seen.add(key)
                            st = e.stat()
                            out.append((e.path, st.st_size, int(st.st_mtime), kind))
                        except OSError:
                            continue
            except OSError:
                continue
    return out


# ---------------------------------------------------------------- proceso de fondo
def _lower_priority():
    try:
        if os.name == "nt":
            import ctypes
            BELOW_NORMAL = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        else:
            os.nice(10)
    except Exception:
        pass


def worker_count():
    """Procesos para leer la biblioteca: 2 si sobra procesador (la primera
    vez, con cientos de videos, rinde casi el doble), si no 1."""
    return 2 if (os.cpu_count() or 1) >= 4 else 1


def run_index(db_path, roots, shard=0, shards=1):
    """Pone el indice al dia con las carpetas: borra lo que ya no esta y lee
    lo nuevo o cambiado (videos primero, lo mas reciente primero). Con
    varios procesos cada uno toma su parte (shard) y solo el primero borra."""
    conn = connect(db_path)
    sfx = str(shard)
    try:
        _set_meta(conn, **{"state" + sfx: "scanning", "done" + sfx: 0, "total" + sfx: 0})
        conn.commit()
        files = walk_library(roots)
        known = {k: (s, m) for k, s, m in conn.execute("SELECT key, size, mtime FROM media")}
        if shard == 0:
            present = {path_key(f[0]) for f in files}
            gone = [k for k in known if k not in present]
            if gone:
                conn.executemany("DELETE FROM media WHERE key=?", [(k,) for k in gone])
                _set_meta(conn, prune=int(get_meta(conn).get("prune", "0")) + 1)
        todo = [f for f in files if known.get(path_key(f[0])) != (f[1], f[2])]
        if shards > 1:
            todo = [f for f in todo if zlib.crc32(path_key(f[0]).encode("utf-8", "replace")) % shards == shard]
        todo.sort(key=lambda f: (f[3] != "v", -f[2]))
        _set_meta(conn, **{"state" + sfx: "indexing", "total" + sfx: len(todo)})
        conn.commit()
        for i, (path, size, mtime, kind) in enumerate(todo, 1):
            try:
                row = index_file(path, kind)
            except Exception as e:
                print("[Indice GPS]", path, e, file=sys.stderr)
                row = _empty_row(kind)
            _insert(conn, path, size, mtime, row)
            _set_meta(conn, **{"done" + sfx: i})
            conn.commit()
        _set_meta(conn, **{"state" + sfx: "done"})
        conn.commit()
        return len(todo)
    finally:
        conn.close()


def worker_main(argv):
    """``--gps-index <base> [--shard i/n] <carpeta> [<carpeta>...]``"""
    global IN_WORKER
    IN_WORKER = True
    args = argv[argv.index("--gps-index") + 1:]
    shard, shards = 0, 1
    if len(args) >= 3 and args[1] == "--shard":
        try:
            shard, shards = (int(x) for x in args[2].split("/"))
        except ValueError:
            return 2
        args = [args[0]] + args[3:]
    if len(args) < 2:
        return 2
    _lower_priority()
    try:
        run_index(args[0], args[1:], shard, shards)
    except Exception as e:
        print("[Indice GPS]", e, file=sys.stderr)
        return 1
    return 0


def worker_command(db_path, roots, shard=0, shards=1):
    """(programa, argumentos) para lanzar un proceso de fondo: la misma app
    (instalada) o el mismo main.py (desde el codigo)."""
    extra = ["--shard", f"{shard}/{shards}"] if shards > 1 else []
    if getattr(sys, "frozen", False):
        return sys.executable, ["--gps-index", db_path, *extra, *roots]
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        w = exe[:-len("python.exe")] + "pythonw.exe"          # sin ventana de consola
        if os.path.exists(w):
            exe = w
    return exe, ["-u", main_py, "--gps-index", db_path, *extra, *roots]


# ---------------------------------------------------------------- consultas desde la app
_COLS = "key, path, kind, t, lat, lon, minlat, maxlat, minlon, maxlon, dist, dur, route, seq"


def load_rows(db_path, since_seq=0):
    """Archivos con GPS guardados despues de since_seq (todos con 0).
    Devuelve (filas, ultimo seq, meta). El recorrido del mapa global queda
    comprimido ("route") hasta que haga falta dibujarlo."""
    if not os.path.exists(db_path):
        return [], since_seq, {}
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            rows = conn.execute(f"SELECT {_COLS} FROM media WHERE ok=1 AND seq>? ORDER BY seq", (since_seq,)).fetchall()
            last = conn.execute("SELECT IFNULL(MAX(seq), 0) FROM media").fetchone()[0]
            meta = get_meta(conn)
        except sqlite3.OperationalError:
            return [], since_seq, {}             # base nueva, todavia sin tablas
    finally:
        conn.close()
    keys = [c.strip() for c in _COLS.split(",")]
    return [dict(zip(keys, r)) for r in rows], max(last, since_seq), meta


def progress_of(meta):
    """(estado, hechos, total) sumando los procesos de fondo."""
    states = [v for k, v in meta.items() if k.startswith("state") and k[5:].isdigit()]
    done = sum(int(v) for k, v in meta.items() if k.startswith("done") and k[4:].isdigit() and v.isdigit())
    total = sum(int(v) for k, v in meta.items() if k.startswith("total") and k[5:].isdigit() and v.isdigit())
    state = "scanning" if "scanning" in states else "indexing" if "indexing" in states else "done"
    return state, done, total


_FAST = {}
_FAST_LOCK = threading.Lock()
_STORED = set()


def fast_track(path, db_path=None):
    """Recorrido para el mapa y el grafico del reproductor: del indice si el
    video ya esta (al instante); si no, se lee como siempre y se guarda en el
    indice para la proxima vez (y para el mapa global)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path_key(path), st.st_size, int(st.st_mtime))
    with _FAST_LOCK:
        if key in _FAST:
            return _FAST[key]
    db_path = db_path or default_db_path()
    track = None
    try:
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path, timeout=3)
            try:
                conn.execute("PRAGMA busy_timeout=3000")
                r = conn.execute("SELECT size, mtime, ok, track, stats FROM media WHERE key=?", (key[0],)).fetchone()
            finally:
                conn.close()
            if r and (r[0], r[1]) == (st.st_size, int(st.st_mtime)):
                track = _track_from_row(r[3] if r[2] else None, r[4])
                if track is None and not r[2]:
                    track = {"points": []}            # sin GPS, ya se sabe: no leer el video
    except Exception as e:
        print("[Indice GPS]", e)
        track = None
    if track is None:
        from .media import read_gopro_track
        track = read_gopro_track(path)
        if not IN_WORKER:
            with _FAST_LOCK:
                first = key not in _STORED
                _STORED.add(key)
            if first:
                try:
                    # sin GPS: {} (no None, que haria leer el video otra vez)
                    row = index_file(path, "v", track=track if track is not None else {})
                    conn = connect(db_path)
                    try:
                        _insert(conn, path, st.st_size, int(st.st_mtime), row)
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as e:
                    print("[Indice GPS] no se guardo", path, e)
    with _FAST_LOCK:
        if len(_FAST) >= 8:
            _FAST.pop(next(iter(_FAST)))
        _FAST[key] = track
    return track
