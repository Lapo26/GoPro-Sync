"""Rescate de videos borrados de una tarjeta SD (solo lectura).

Recorre el espacio LIBRE de la tarjeta (exFAT o FAT32) buscando el comienzo
de archivos MP4 ("ftyp") y copia lo que encuentra a otra carpeta. Nunca
escribe en la tarjeta: el volumen se abre en modo lectura. Lo rescatado
suele no tener indice (moov) y se arregla despues con "Reparar".

En Windows leer el volumen crudo pide permisos de administrador: la app se
vuelve a abrir elevada solo para este escaneo (main.py --recover-scan) y
avisa el avance por un archivo de estado en la carpeta de salida."""
import json
import os
import struct
import sys
import time
import traceback

SIGS = (b"ftypmp41", b"ftypmp42", b"ftypisom", b"ftypavc1")
MAX_EXTRACT = 4 * 1024 ** 3
STATUS_FILE = "rescate_estado.json"
CANCEL_FILE = "rescate_cancelar"


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


class Volume:
    """Lectura de un volumen exFAT / FAT32 (o de una imagen de disco)."""

    def __init__(self, dev):
        self.dev = dev
        boot = self.read_at(0, 512)
        if boot[3:11] == b"EXFAT   ":
            self.kind = "exFAT"
            bps = 1 << boot[108]
            self.csize = bps * (1 << boot[109])
            fat_off, fat_len = _u32(boot, 80) * bps, _u32(boot, 84) * bps
            self.heap_off = _u32(boot, 88) * bps
            self.ccount = _u32(boot, 92)
            root = _u32(boot, 96)
            self.fat = self.read_at(fat_off, fat_len)
            self.bitmap = self._exfat_bitmap(root)
        elif boot[82:90] == b"FAT32   ":
            self.kind = "FAT32"
            bps, spc = _u16(boot, 11), boot[13]
            reserved, nfats = _u16(boot, 14), boot[16]
            fatsz = _u32(boot, 36)
            total = _u32(boot, 32) or _u16(boot, 19)
            self.csize = bps * spc
            self.heap_off = (reserved + nfats * fatsz) * bps
            self.ccount = (total - reserved - nfats * fatsz) // spc
            self.fat = self.read_at(reserved * bps, fatsz * bps)
            self.bitmap = None
        else:
            raise ValueError("formato")

    def read_at(self, off, size):
        self.dev.seek(off)
        return self.dev.read(size)

    def read_clusters(self, n, count):
        return self.read_at(self.heap_off + (n - 2) * self.csize, count * self.csize)

    def _chain(self, first, length=None):
        out, c = [], first
        need = None if length is None else (length + self.csize - 1) // self.csize
        while 2 <= c < 0xFFFFFFF7:
            out.append(c)
            if need is not None and len(out) >= need:
                break
            nxt = _u32(self.fat, c * 4)
            if nxt == 0 and need is not None:
                return list(range(first, first + need))
            c = nxt
        return out

    def _exfat_bitmap(self, root):
        for c in self._chain(root):
            d = self.read_clusters(c, 1)
            for i in range(0, len(d), 32):
                if d[i] == 0x81:
                    first, length = _u32(d, i + 20), _u64(d, i + 24)
                    return b"".join(self.read_clusters(x, 1) for x in self._chain(first, length))[:length]
                if d[i] == 0x00:
                    break
        raise ValueError("bitmap")

    def is_free(self, c):
        if self.bitmap is not None:
            i = c - 2
            return not (self.bitmap[i >> 3] >> (i & 7)) & 1
        return (_u32(self.fat, c * 4) & 0x0FFFFFFF) == 0

    def free_runs(self):
        runs, start = [], None
        for c in range(2, self.ccount + 2):
            if self.is_free(c):
                if start is None:
                    start = c
            elif start is not None:
                runs.append((start, c - start))
                start = None
        if start is not None:
            runs.append((start, self.ccount + 2 - start))
        return runs


def scan(dev, outdir, progress=lambda pct, found: None, cancel=lambda: False, log=print):
    """Escanea el espacio libre y extrae a outdir lo que parezca un video.
    Devuelve la lista de archivos rescatados [(ruta, bytes)]."""
    vol = Volume(dev)
    csize = vol.csize
    runs = vol.free_runs()
    free_total = sum(n for _s, n in runs) or 1
    log(f"{vol.kind}: cluster {csize} B, libres {free_total} ({free_total * csize / 1e9:.2f} GB)")
    zero, ff = bytes(csize), b"\xff" * csize
    hits, done = [], 0
    chunk = max(1, (32 * 1024 * 1024) // csize)
    for rstart, rlen in runs:
        for base in range(rstart, rstart + rlen, chunk):
            if cancel():
                return []
            n = min(chunk, rstart + rlen - base)
            buf = vol.read_clusters(base, n)
            for sig in SIGS:
                pos = buf.find(sig)
                while pos != -1:
                    if pos >= 4:
                        cnum = base + (pos - 4) // csize
                        off = (pos - 4) % csize
                        hdr = buf[pos - 4:pos + 64]
                        hits.append({"cluster": cnum, "offset": off, "gopro": b"GPRO" in hdr or b"GoPro" in hdr})
                    pos = buf.find(sig, pos + 1)
            done += n
            progress(min(95.0, done * 95.0 / free_total), len(hits))
    hits.sort(key=lambda h: (h["cluster"], h["offset"]))
    starts = {h["cluster"] for h in hits}
    out = []
    os.makedirs(outdir, exist_ok=True)
    for idx, h in enumerate(hits):
        if cancel():
            break
        c, written = h["cluster"], 0
        name = os.path.join(outdir, f"rescatado_{idx + 1:03d}{'_GoPro' if h['gopro'] else ''}.mp4")
        with open(name, "wb") as f:
            first = True
            while c < vol.ccount + 2 and vol.is_free(c) and written < MAX_EXTRACT:
                if not first and c in starts:
                    break                      # empieza otro archivo
                cl = vol.read_clusters(c, 1)
                if cl == zero or cl == ff:
                    break
                if first:
                    cl = cl[h["offset"]:]
                    first = False
                f.write(cl)
                written += len(cl)
                c += 1
        if written < 64 * 1024:
            os.remove(name)                    # migajas: no sirven
            continue
        out.append((name, written))
        log(f"rescatado {os.path.basename(name)}: {written / 1e6:.1f} MB")
        progress(95.0 + 5.0 * (idx + 1) / max(1, len(hits)), len(out))
    return out


def open_volume(drive):
    """Abre la unidad (p. ej. "F:") en modo lectura cruda (Windows)."""
    letter = drive.strip().rstrip("\\/").rstrip(":")
    return open(r"\\.\%s:" % letter, "rb", buffering=0)


def is_admin():
    if os.name != "nt":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def launch_elevated_scan(drive, outdir):
    """Abre otra copia de la app como administrador solo para escanear."""
    import ctypes
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, []
    else:
        exe = sys.executable
        args = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")]
    args += ["--recover-scan", drive, outdir]
    params = " ".join(f'"{a}"' for a in args)
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
    return r > 32


def write_status(outdir, **data):
    try:
        tmp = os.path.join(outdir, STATUS_FILE + ".part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(outdir, STATUS_FILE))
    except Exception:
        pass


def read_status(outdir):
    try:
        with open(os.path.join(outdir, STATUS_FILE), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def run_scan_cli(drive, outdir):
    """Punto de entrada de la copia elevada (--recover-scan)."""
    os.makedirs(outdir, exist_ok=True)
    write_status(outdir, state="running", pct=0.0, found=0)
    cancel_file = os.path.join(outdir, CANCEL_FILE)
    last = [0.0]

    def progress(pct, found):
        if time.monotonic() - last[0] > 0.5:
            last[0] = time.monotonic()
            write_status(outdir, state="running", pct=pct, found=found)
    logf = open(os.path.join(outdir, "rescate_log.txt"), "w", encoding="utf-8")

    def log(msg):
        logf.write(msg + "\n")
        logf.flush()
    try:
        with open_volume(drive) as dev:
            files = scan(dev, outdir, progress, lambda: os.path.exists(cancel_file), log)
        if os.path.exists(cancel_file):
            write_status(outdir, state="cancelled")
        else:
            write_status(outdir, state="done", files=[f for f, _n in files])
    except ValueError:
        write_status(outdir, state="error", error="format")
    except Exception as e:
        log(traceback.format_exc())
        write_status(outdir, state="error", error=str(e))
    finally:
        logf.close()


def recover_job(drive, outdir, lang):
    """Trabajo para Actividad: lanza el escaneo elevado y sigue su avance."""
    from .i18n import tr

    def run(progress, cancel):
        for name in (STATUS_FILE, CANCEL_FILE):
            try:
                os.remove(os.path.join(outdir, name))
            except OSError:
                pass
        os.makedirs(outdir, exist_ok=True)
        if is_admin():
            import threading
            th = threading.Thread(target=run_scan_cli, args=(drive, outdir), daemon=True)
            th.start()
        elif os.name == "nt":
            if not launch_elevated_scan(drive, outdir):
                return False, tr(lang, "rec_no_admin")
        else:
            return False, tr(lang, "rec_windows_only")
        t0 = time.monotonic()
        while True:
            st = read_status(outdir)
            if cancel():
                open(os.path.join(outdir, CANCEL_FILE), "w").close()
                return False, "cancelled"
            if st is None and time.monotonic() - t0 > 90:
                return False, tr(lang, "rec_no_admin")
            if st:
                if st.get("state") == "running":
                    progress(st.get("pct", 0.0))
                elif st.get("state") == "done":
                    files = st.get("files") or []
                    if not files:
                        return True, {"note": tr(lang, "rec_none")}
                    return True, {"output": files[0], "files": files,
                                  "note": tr(lang, "rec_found", n=len(files))}
                elif st.get("state") == "error":
                    return False, tr(lang, "rec_format") if st.get("error") == "format" else st.get("error", "?")
                elif st.get("state") == "cancelled":
                    return False, "cancelled"
            time.sleep(0.5)
    return {"name": drive, "size": 1, "run": run}
