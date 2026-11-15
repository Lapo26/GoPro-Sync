"""Herramientas de video con ffmpeg: version liviana, unir partes, reparar."""
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import traceback
from PySide6.QtCore import QStandardPaths, QThread, Signal
from .env import APP_DIR, _hidden_startupinfo
from .i18n import _human_size, tr
from .winshell import move_to_recycle_bin
from .media import _read_moov, is_broken_mp4, read_mp4_info
from .fileops import _CopyCancelled, _copy_creation_time

_FFMPEG = {"exe": None}


def ffmpeg_exe():
    """ffmpeg incluido con la app (paquete imageio-ffmpeg)."""
    if _FFMPEG["exe"] is None:
        try:
            import imageio_ffmpeg
            _FFMPEG["exe"] = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            print("[ffmpeg]", e)
            _FFMPEG["exe"] = ""
        if not _FFMPEG["exe"] or not os.path.exists(_FFMPEG["exe"]):
            # app compilada: buscarlo junto al programa
            import glob
            base = APP_DIR
            found = [x for x in glob.glob(os.path.join(base, "imageio_ffmpeg", "binaries", "ffmpeg*"))
                     if not x.endswith((".md", ".txt"))]
            _FFMPEG["exe"] = found[0] if found else ""
    return _FFMPEG["exe"] or None


def run_ffmpeg(args, duration, progress=None, cancel=lambda: False, stall_s=180):
    """Corre ffmpeg con progreso real (por tiempo de video procesado).
    Devuelve (ok, mensaje_de_error). Cancelar o 'trabado' cortan el proceso."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg")
    errlog = tempfile.NamedTemporaryFile(prefix="gpsync_ff_", suffix=".log", delete=False)
    errlog.close()
    cmd = [exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"] + list(args)
    with open(errlog.name, "w", encoding="utf-8", errors="replace") as ferr:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ferr, text=True, encoding="utf-8",
                                errors="replace", startupinfo=_hidden_startupinfo(), bufsize=1)
        last = {"t": time.monotonic()}
        done = {"v": False}

        def watchdog():
            while not done["v"]:
                time.sleep(0.3)
                if cancel() or time.monotonic() - last["t"] > stall_s:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            for line in proc.stdout:
                last["t"] = time.monotonic()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        us = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        continue
                    if progress and duration:
                        progress(max(0.0, min(99.9, us / 1e6 / duration * 100.0)))
            proc.wait()
        finally:
            done["v"] = True
    try:
        err = open(errlog.name, encoding="utf-8", errors="replace").read().strip()
        os.remove(errlog.name)
    except Exception:
        err = ""
    if cancel():
        return False, "cancelled"
    return proc.returncode == 0, (err.splitlines()[-1] if err else f"codigo {proc.returncode}")


class ToolJobThread(QThread):
    """Corre una lista de trabajos (unir, version liviana, reparar...) con la
    misma interfaz que una copia: se ve en Actividad con progreso y Cancelar."""
    progress = Signal(int, int, str, float)
    finished_sync = Signal(bool, str, dict)
    plan = Signal(object)

    def __init__(self, jobs, lang, parent=None):
        super().__init__(parent)
        self.jobs, self.lang = jobs, lang       # [{"name", "size", "run": f(progress, cancel) -> (ok, info)}]
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        self.plan.emit([int(j.get("size") or 1) for j in self.jobs])
        ok_n, errors, outputs, notes = 0, [], [], []
        total = len(self.jobs)
        for idx, job in enumerate(self.jobs, start=1):
            if self._cancel:
                break
            name = job["name"]
            self.progress.emit(idx, total, name, 0.0)
            try:
                ok, info = job["run"](lambda pct, i=idx, n=name: self.progress.emit(i, total, n, pct),
                                      lambda: self._cancel)
            except Exception as e:
                traceback.print_exc()
                ok, info = False, str(e)
            if self._cancel:
                break
            if ok:
                ok_n += 1
                self.progress.emit(idx, total, name, 100.0)
                if isinstance(info, dict):
                    if info.get("output"):
                        outputs.append(info["output"])
                    if info.get("note"):
                        notes.append(info["note"])
            else:
                errors.append((name, info if isinstance(info, str) else tr(self.lang, "err_unknown")))
        data = {"copied": ok_n, "total": total, "errors": len(errors), "error_list": errors, "outputs": outputs}
        if self._cancel:
            data["cancelled"] = True
            self.finished_sync.emit(False, tr(self.lang, "sync_cancelled_msg", copied=ok_n, total=total), data)
            return
        msg = " ".join(notes) if (notes and total == 1) else tr(self.lang, "tools_done", ok=ok_n, total=total)
        if errors:
            msg += tr(self.lang, "sync_errors_suffix", errors=len(errors))
        self.finished_sync.emit(not errors, msg, data)


def share_dir():
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation) or os.path.expanduser("~")
    d = os.path.join(base, "GoPro Sync")
    os.makedirs(d, exist_ok=True)
    return d


def light_version_job(src, lang, out_dir=None):
    """Video chico para compartir (WhatsApp, mail): 720p H.264, inicio rapido.
    Se guarda en out_dir (la carpeta elegida) o en la carpeta Videos/GoPro Sync."""
    def run(progress, cancel):
        info = read_mp4_info(src)
        base = os.path.splitext(os.path.basename(src))[0]
        d = out_dir or share_dir()
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, f"{base}_liviano.mp4")
        n = 2
        while os.path.exists(out):                       # no pisar una version anterior
            out = os.path.join(d, f"{base}_liviano_{n}.mp4")
            n += 1
        part = out + ".part.mp4"
        ok, err = run_ffmpeg(["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:'min(720,ih)'",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p",
                              "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", part],
                             info.get("duration") or 0, progress, cancel)
        if not ok:
            try:
                os.remove(part)
            except OSError:
                pass
            return False, tr(lang, "ffmpeg_failed", e=err) if err != "cancelled" else err
        os.replace(part, out)
        return True, {"output": out, "note": tr(lang, "light_done", size=_human_size(os.path.getsize(out)))}
    return {"name": os.path.basename(src), "size": os.path.getsize(src), "run": run}


# ---- videos partidos (capitulos de la GoPro)
_CHAPTER_RE = re.compile(r"^(GOPR|GP(\d\d))(\d{4})\.MP4$", re.IGNORECASE)


def find_chapter_groups(root):
    """Grupos GOPR1234 + GP011234 + GP021234... en la misma carpeta."""
    groups = {}
    for d, _dirs, names in os.walk(root):
        for n in names:
            m = _CHAPTER_RE.match(n)
            if m:
                order = 0 if m.group(1).upper() == "GOPR" else int(m.group(2))
                groups.setdefault((d, m.group(3)), []).append((order, os.path.join(d, n)))
    out = []
    for (d, num), parts in groups.items():
        if len(parts) < 2:
            continue
        parts.sort()
        if parts[0][0] != 0:
            continue                  # falta la primera parte
        paths = [x for _o, x in parts]
        joined = os.path.join(d, f"GOPR{num}_unido.MP4")
        orders = [o for o, _x in parts]
        missing = [f"GP{k:02d}{num}.MP4" for k in range(1, max(orders) + 1) if k not in orders]
        out.append({"name": os.path.basename(paths[0]), "parts": paths, "output": joined, "missing": missing,
                    "size": sum(os.path.getsize(x) for x in paths),
                    "damaged": [os.path.basename(x) for x in paths if is_broken_mp4(x)],
                    "done": os.path.exists(joined)})
    out.sort(key=lambda g: g["parts"][0])
    return out


def chapter_group_of(path):
    m = _CHAPTER_RE.match(os.path.basename(path))
    if not m:
        return None
    d = os.path.dirname(path)
    for g in find_chapter_groups_in_dir(d):
        if os.path.normcase(path) in [os.path.normcase(x) for x in g["parts"]]:
            return g
    return None


def find_chapter_groups_in_dir(d):
    try:
        names = os.listdir(d)
    except OSError:
        return []
    groups = {}
    for n in names:
        m = _CHAPTER_RE.match(n)
        if m:
            order = 0 if m.group(1).upper() == "GOPR" else int(m.group(2))
            groups.setdefault(m.group(3), []).append((order, os.path.join(d, n)))
    out = []
    for num, parts in groups.items():
        parts.sort()
        if len(parts) >= 2 and parts[0][0] == 0:
            paths = [x for _o, x in parts]
            orders = [o for o, _x in parts]
            out.append({"name": os.path.basename(paths[0]), "parts": paths,
                        "missing": [f"GP{k:02d}{num}.MP4" for k in range(1, max(orders) + 1) if k not in orders],
                        "output": os.path.join(d, f"GOPR{num}_unido.MP4"),
                        "size": sum(os.path.getsize(x) for x in paths),
                        "damaged": [os.path.basename(x) for x in paths if is_broken_mp4(x)],
                        "done": os.path.exists(os.path.join(d, f"GOPR{num}_unido.MP4"))})
    return out


def selection_join_group(paths, lang):
    """Arma un grupo para unir videos elegidos a mano. Devuelve (grupo, error).
    Solo se unen sin recomprimir si tienen la misma resolucion, velocidad de
    cuadros y formatos (si no, el video resultante saldria roto)."""
    if len(paths) < 2:
        return None, tr(lang, "join_need_two")
    def when(x):
        try:
            return (os.path.getmtime(x), os.path.basename(x).upper())
        except OSError:
            return (0, os.path.basename(x).upper())
    paths = sorted(paths, key=when)                 # orden de grabacion
    sig_first = None
    for x in paths:
        if is_broken_mp4(x):
            return None, tr(lang, "join_damaged_sel", name=os.path.basename(x))
        info = read_mp4_info(x)
        v, a = info.get("video") or {}, info.get("audio") or {}
        sig = (v.get("codec"), v.get("w"), v.get("h"), v.get("timescale"), v.get("delta"), a.get("codec"), a.get("timescale"))
        if sig_first is None:
            sig_first = (sig, x)
        elif sig != sig_first[0]:
            return None, tr(lang, "join_incompatible", a=os.path.basename(sig_first[1]), b=os.path.basename(x))
    first = paths[0]
    base = os.path.splitext(os.path.basename(first))[0]
    out = os.path.join(os.path.dirname(first), f"{base}_unido.MP4")
    n = 2
    while os.path.exists(out):
        out = os.path.join(os.path.dirname(first), f"{base}_unido_{n}.MP4")
        n += 1
    return {"name": os.path.basename(first), "parts": paths, "output": out, "damaged": [], "missing": [],
            "size": sum(os.path.getsize(x) for x in paths), "done": False}, None


def join_job(group, lang, recycle_parts, on_joined=None):
    """Une las partes sin recomprimir (video, audio y la pista GPS)."""
    def run(progress, cancel):
        out = group["output"]
        if os.path.exists(out):
            return True, {"note": tr(lang, "join_already", name=os.path.basename(out))}
        duration = sum((read_mp4_info(x).get("duration") or 0) for x in group["parts"])
        lst = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        for x in group["parts"]:
            lst.write("file '" + x.replace("\\", "/").replace("'", "'\\''") + "'\n")
        lst.close()
        part = out + ".part.mp4"
        try:
            ok, err = run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst.name, "-map", "0:v", "-map", "0:a?",
                                  "-map", "0:d?", "-c", "copy", "-copy_unknown", part], duration, progress, cancel)
        finally:
            try:
                os.remove(lst.name)
            except OSError:
                pass
        if not ok or not os.path.exists(part) or os.path.getsize(part) < group["size"] * 0.9:
            try:
                os.remove(part)
            except OSError:
                pass
            return False, err if err == "cancelled" else tr(lang, "ffmpeg_failed", e=err)
        os.replace(part, out)
        try:
            shutil.copystat(group["parts"][0], out)
            _copy_creation_time(group["parts"][0], out)
        except Exception:
            pass
        if on_joined:
            on_joined(group)
        if recycle_parts:
            for x in group["parts"]:
                move_to_recycle_bin(x)
        return True, {"output": out, "note": tr(lang, "join_one_done", name=os.path.basename(out))}
    return {"name": group["name"], "size": group["size"], "run": run}


# ---- reparar videos sin indice (grabacion cortada): rescate de la imagen
def _avcc_params(path):
    """SPS, PPS y fps (como fraccion para ffmpeg) de un video sano."""
    moov = _read_moov(path)
    if not moov:
        return None
    i = moov.find(b"avcC")
    if i < 0:
        return None
    a = i + 4
    p = a + 6
    sps, pps = [], []
    try:
        for _ in range(moov[a + 5] & 0x1F):
            ln = struct.unpack(">H", moov[p:p + 2])[0]
            sps.append(bytes(moov[p + 2:p + 2 + ln]))
            p += 2 + ln
        n = moov[p]
        p += 1
        for _ in range(n):
            ln = struct.unpack(">H", moov[p:p + 2])[0]
            pps.append(bytes(moov[p + 2:p + 2 + ln]))
            p += 2 + ln
    except Exception:
        return None
    fps = None
    try:
        out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", startupinfo=_hidden_startupinfo(), timeout=30).stderr
        mm = re.search(r"([\d.]+) fps", out)
        if mm:
            val = float(mm.group(1))
            known = {23.98: "24000/1001", 29.97: "30000/1001", 59.94: "60000/1001", 119.88: "120000/1001",
                     47.95: "48000/1001", 239.76: "240000/1001"}
            fps = next((v for k, v in known.items() if abs(k - val) < 0.02), f"{val:g}")
    except Exception:
        pass
    return {"sps": sps, "pps": pps, "fps": fps}


def _repair_references(src):
    """Videos sanos de la misma carpeta, empezando por los de numeracion mas cercana."""
    d = os.path.dirname(src)
    digits = re.sub(r"\D", "", os.path.basename(src))[-4:]
    try:
        names = [n for n in os.listdir(d) if n.lower().endswith(".mp4") and os.path.join(d, n) != src
                 and "_reparado" not in n.lower()]
    except OSError:
        return []
    def dist(n):
        dd = re.sub(r"\D", "", n)[-4:]
        return abs(int(dd) - int(digits)) if dd and digits else 99999
    out = []
    for n in sorted(names, key=dist):
        full = os.path.join(d, n)
        if not is_broken_mp4(full):
            out.append(full)
        if len(out) >= 3:
            break
    return out


_AUD = b"\x00\x00\x00\x02\x09"      # separador de cuadro (Access Unit Delimiter, 2 bytes)


def _parse_frame(buf, i, end, known):
    """Valida un cuadro que empieza en i (AUD + SEI/SPS/PPS + imagen) y
    devuelve (fin, [NALs]) o None. La GoPro escribe cada cuadro asi y entre
    cuadros mete audio/GPS: solo se acepta lo que encaja exacto."""
    nals = [buf[i + 4:i + 6]]
    j = i + 6
    slices = 0
    while j + 6 <= end:
        ln = struct.unpack(">I", buf[j:j + 4])[0]
        h = buf[j + 4]
        t = h & 0x1F
        if h & 0x80 or j + 4 + ln > end:
            break
        if t == 6 and 2 <= ln <= 1024 and not slices:
            pass
        elif t in (7, 8) and not slices:
            if bytes(buf[j + 4:j + 4 + ln]) not in known:
                return None                    # SPS/PPS que no es de esta camara: no es un cuadro
        elif t in (1, 5) and 64 <= ln <= 12_000_000:
            first_mb_zero = bool(buf[j + 5] & 0x80)
            if (slices == 0) != first_mb_zero:  # el primer slice arranca en el macrobloque 0
                break
            if t == 5 and not (h & 0x60):
                return None
            slices += 1
        else:
            break
        nals.append(buf[j + 4:j + 4 + ln])
        j += 4 + ln
    return (j, nals) if slices else None


def _rescue_h264(src, params, sink, progress, cancel, max_bytes=None):
    """Recorre el mdat del video roto y manda a 'sink' el H.264 (Annex B),
    cuadro por cuadro."""
    known = set(params["sps"]) | set(params["pps"])
    start = b"\x00\x00\x00\x01"
    sink(b"".join(start + x for x in params["sps"] + params["pps"]))
    size = os.path.getsize(src)
    frames = 0
    with open(src, "rb") as f:
        pos = 0
        while True:                                  # saltar las cajas hasta mdat
            f.seek(pos)
            h = f.read(16)
            if len(h) < 8:
                return 0
            bs = struct.unpack(">I", h[:4])[0]
            if h[4:8] == b"mdat":
                pos += 16 if bs == 1 else 8
                break
            if bs < 8:
                return 0
            pos += bs
        f.seek(pos)
        carry = b""
        done = pos
        out = bytearray()
        while True:
            if cancel():
                raise _CopyCancelled()
            want = 64 * 1024 * 1024
            if max_bytes is not None:
                want = max(0, min(want, max_bytes - (done - pos)))
            chunk = f.read(want) if want else b""
            buf = carry + chunk
            end = len(buf)
            final = not chunk
            limit = end if final else max(0, end - 13_000_000)   # un cuadro entero tiene que entrar
            consumed = 0
            i = buf.find(_AUD, 0)
            while 0 <= i < limit:
                fr = _parse_frame(buf, i, end, known)
                if fr is None:
                    i = buf.find(_AUD, i + 1)
                    continue
                j, nals = fr
                consumed = j
                for nal in nals:
                    out += start + nal
                frames += 1
                if len(out) > 8 * 1024 * 1024:
                    sink(bytes(out))
                    out = bytearray()
                i = buf.find(_AUD, j)
            if out:
                sink(bytes(out))
                out = bytearray()
            done += len(chunk)
            if progress:
                progress(min(99.0, (done - pos) * 100.0 / max(1, size - pos)))
            if final:
                break
            k = i if 0 <= i < end else limit
            carry = buf[max(min(k, limit), consumed):]     # sin repetir el final del ultimo cuadro
    return frames


def repair_job(src, lang, out_dir=None):
    """Recupera la imagen de un video al que le falta el indice (moov)."""
    def run(progress, cancel):
        if os.path.getsize(src) == 0:
            return False, tr(lang, "repair_empty")
        refs = _repair_references(src)
        if not refs:
            return False, tr(lang, "repair_no_ref")
        base = os.path.splitext(os.path.basename(src))[0]
        out = os.path.join(out_dir or os.path.dirname(src), f"{base}_reparado.MP4")
        part = out + ".part.mp4"
        def mux(params, part, max_bytes, prog):
            args = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "h264"]
            if params["fps"]:
                args += ["-r", params["fps"]]
            args += ["-i", "pipe:0", "-c", "copy", part]
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, startupinfo=_hidden_startupinfo())
            try:
                _rescue_h264(src, params, proc.stdin.write, prog, cancel, max_bytes)
                proc.stdin.close()
                proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            return proc.returncode == 0

        def decodes(path):
            chk = subprocess.run([ffmpeg_exe(), "-hide_banner", "-v", "error", "-t", "3", "-i", path, "-f", "null", "-"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 startupinfo=_hidden_startupinfo(), timeout=120)
            return chk.stderr.count("\n") < 30

        def drop(path):
            try:
                os.remove(path)
            except OSError:
                pass

        # 1) elegir la referencia probando solo el comienzo del video (rapido)
        chosen = None
        for ref in refs:
            params = _avcc_params(ref)
            if not params or not params["sps"]:
                continue
            try:
                ok = mux(params, part, 200 * 1024 * 1024, None)
            except _CopyCancelled:
                drop(part)
                return False, "cancelled"
            except Exception:
                ok = False
            good = ok and (read_mp4_info(part).get("duration") or 0) > 1 and decodes(part)
            drop(part)
            if good:
                chosen = params
                break
        if chosen is None:
            return False, tr(lang, "repair_failed")
        # 2) el archivo completo, una sola vez
        try:
            ok = mux(chosen, part, None, progress)
        except _CopyCancelled:
            drop(part)
            return False, "cancelled"
        except Exception:
            ok = False
        dur = read_mp4_info(part).get("duration") if ok and os.path.exists(part) else 0
        if ok and dur and dur > 1 and decodes(part):
            os.replace(part, out)
            try:
                shutil.copystat(src, out)
                _copy_creation_time(src, out)
            except Exception:
                pass
            mins, secs = int(dur // 60), int(dur % 60)
            return True, {"output": out, "note": tr(lang, "repair_done", name=os.path.basename(out),
                                                   dur=f"{mins}:{secs:02d}")}
        drop(part)
        return False, tr(lang, "repair_failed")
    return {"name": os.path.basename(src), "size": os.path.getsize(src) or 1, "run": run}


# ---- fragmentos de un video (para mandar a alguien)
CLIP_MODES = ("share", "hd", "original")


def _fmt_clip_time(s):
    s = int(max(0, s))
    return f"{s // 60:02d}m{s % 60:02d}s"


def _unique_path(d, base, ext):
    out = os.path.join(d, f"{base}{ext}")
    n = 2
    while os.path.exists(out):
        out = os.path.join(d, f"{base}_{n}{ext}")
        n += 1
    return out


def clip_job(src, start_s, end_s, mode, lang, out_dir=None):
    """Recorta [start_s, end_s] de un video.
    mode: "share" = 720p liviano (WhatsApp, mail), "hd" = 1080p de buena
    calidad, "original" = sin recomprimir (instantaneo, pero el corte cae en
    el cuadro clave mas cercano, uno o dos segundos antes)."""
    start_s, end_s = max(0.0, float(start_s)), float(end_s)
    dur = max(0.1, end_s - start_s)

    def run(progress, cancel):
        base = os.path.splitext(os.path.basename(src))[0]
        d = out_dir or share_dir()
        os.makedirs(d, exist_ok=True)
        out = _unique_path(d, f"{base}_{_fmt_clip_time(start_s)}-{_fmt_clip_time(end_s)}", ".mp4")
        part = out + ".part.mp4"
        args = ["-ss", f"{start_s:.3f}", "-i", src, "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?"]
        if mode == "original":
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        elif mode == "hd":
            args += ["-vf", "scale=-2:'min(1080,ih)'", "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
        else:
            args += ["-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
        args += ["-movflags", "+faststart", part]
        ok, err = run_ffmpeg(args, dur, progress, cancel)
        if not ok or not os.path.exists(part) or os.path.getsize(part) == 0:
            try:
                os.remove(part)
            except OSError:
                pass
            return False, tr(lang, "ffmpeg_failed", e=err) if err != "cancelled" else err
        os.replace(part, out)
        try:
            shutil.copystat(src, out)          # misma fecha que el video: va al mismo dia en la galeria
            _copy_creation_time(src, out)
        except Exception:
            pass
        return True, {"output": out, "note": tr(lang, "clip_done", size=_human_size(os.path.getsize(out)))}
    return {"name": os.path.basename(src), "size": max(1, int(dur * 1000)), "run": run}


# ---- secuencias de fotos (time-lapse / rafaga) a video o GIF
def _concat_list(frames):
    """Lista para el concat de ffmpeg. Cada foto se lee como un archivo
    aparte (con un patron %04d un cuadro que falta cortaria el video, y en
    Windows ffmpeg no acepta comodines)."""
    fd, path = tempfile.mkstemp(prefix="gpsync_seq_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for p in frames:
            q = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{q}'\n")
    return path


def sequence_filters(opts):
    """Cadena de filtros de video para la exportacion (sin el retiempo)."""
    fps = int(opts.get("fps") or 30)
    width = int(opts.get("width") or 1920)
    vf = [f"setpts=N/({fps}*TB)"]           # un cuadro por foto, exacto
    if opts.get("crop169"):
        vf.append("crop=iw:'min(ih,trunc(iw*9/32)*2)'")
    # las fotos JPEG son de rango completo: se pasa a rango de video normal
    # (si no, en algunos celulares el video se ve lavado)
    vf.append(f"scale='min({width},iw)':-2:flags=lanczos:out_range=tv")
    if opts.get("deflicker") and opts.get("format") != "gif":
        vf.append("deflicker=size=7:mode=pm")   # parpadeo de exposicion entre fotos
    return ",".join(vf)


def sequence_export_name(seq, fmt):
    from datetime import datetime
    try:
        day = datetime.fromtimestamp(os.path.getmtime(seq["frames"][0])).strftime("%Y-%m-%d")
    except OSError:
        day = ""
    kind = "Burst" if seq.get("kind") == "burst" else "TimeLapse"
    base = f"{kind}_G{seq['group']}" + (f"_{day}" if day else "")
    return base, (".gif" if fmt == "gif" else ".mp4")


def sequence_video_job(seq, opts, lang, out_dir=None):
    """Arma un video MP4 (H.264) o un GIF con las fotos de la secuencia.
    opts: format ("mp4"/"gif"), fps, width, crop169, deflicker."""
    frames = list(seq["frames"])
    fmt = opts.get("format") or "mp4"
    fps = int(opts.get("fps") or 30)
    size = 0
    for p in frames:
        try:
            size += os.path.getsize(p)
        except OSError:
            pass

    def run(progress, cancel):
        d = out_dir or seq.get("dir") or share_dir()
        os.makedirs(d, exist_ok=True)
        base, ext = sequence_export_name(seq, fmt)
        out = _unique_path(d, base, ext)
        part = out + ".part" + ext
        lst = _concat_list(frames)
        vf = sequence_filters(opts)
        args = ["-f", "concat", "-safe", "0", "-i", lst]
        if fmt == "gif":
            args += ["-filter_complex",
                     f"[0:v]{vf},split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                     "-r", str(fps), "-loop", "0", "-f", "gif", part]
        else:
            from datetime import datetime, timezone
            try:
                created = datetime.fromtimestamp(os.path.getmtime(frames[0]), timezone.utc)
                meta = ["-metadata", "creation_time=" + created.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
            except OSError:
                meta = []
            args += ["-vf", vf + ",format=yuv420p", "-r", str(fps), "-c:v", "libx264", "-preset", "medium",
                     "-crf", "18", "-tag:v", "avc1", "-movflags", "+faststart"] + meta + ["-f", "mp4", part]
        try:
            ok, err = run_ffmpeg(args, len(frames) / float(fps), progress, cancel)
        finally:
            try:
                os.remove(lst)
            except OSError:
                pass
        if not ok or not os.path.exists(part) or os.path.getsize(part) == 0:
            try:
                os.remove(part)
            except OSError:
                pass
            return False, tr(lang, "ffmpeg_failed", e=err) if err != "cancelled" else err
        os.replace(part, out)
        try:
            shutil.copystat(frames[0], out)    # fecha de la primera foto: queda en ese dia de la galeria
            _copy_creation_time(frames[0], out)
        except Exception:
            pass
        secs = len(frames) / float(fps)
        return True, {"output": out, "note": tr(lang, "seq_export_done", name=os.path.basename(out),
                                               dur=f"{int(secs // 60)}:{int(secs % 60):02d}",
                                               size=_human_size(os.path.getsize(out)))}
    return {"name": f"G{seq['group']} · {len(frames)}", "size": size or 1, "run": run}
