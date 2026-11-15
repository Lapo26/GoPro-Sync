"""Video con los datos encima (velocidad, altura, distancia, hora y un
minimapa del recorrido), como los "gauges" de GoPro Quik.

Los datos se dibujan con Qt en una franja transparente (10 cuadros por
segundo) que se manda a ffmpeg por un pipe y se superpone al video: no
depende de los filtros de texto de ffmpeg (que cambian segun la plataforma)
ni deja miles de imagenes temporales en el disco."""
import bisect
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen

from .env import _hidden_startupinfo
from .fileops import _copy_creation_time
from .i18n import _human_size, tr
from .media import _haversine, read_gopro_track, read_mp4_info
from .videotools import _unique_path, ffmpeg_exe, share_dir

OVERLAY_FPS = 10
DEFAULT_OPTS = {"speed": True, "alt": True, "dist": True, "clock": True, "map": True,
                "units": "kmh", "height": 1080, "accent": "#00f2fe"}


class TelemetryTimeline:
    """Valores de la telemetria en cualquier segundo del video."""

    def __init__(self, track, duration):
        self.pts = track["points"]
        n = len(self.pts)
        fr = track.get("fracs") or []
        self.fracs = fr if len(fr) == n else [i / max(1, n - 1) for i in range(n)]
        self.duration = duration or 1.0
        self.utcs = track.get("utcs") or [None] * n
        # velocidad suavizada (~1 s) y distancia acumulada que ignora el temblor del GPS
        self.speed = []
        k = 9
        acc = 0.0
        for i, p in enumerate(self.pts):
            acc += p[3]
            if i >= k:
                acc -= self.pts[i - k][3]
            self.speed.append(acc / min(i + 1, k) * 3.6)
        self.cum, total, anchor = [], 0.0, None
        for p in self.pts:
            if anchor is None:
                anchor = p
            else:
                d = _haversine(anchor, p)
                if d >= 3:
                    total += d
                    anchor = p
            self.cum.append(total)
        self.vmax = max(self.speed) if self.speed else 1.0
        lats = [p[0] for p in self.pts]
        lons = [p[1] for p in self.pts]
        self.box = (min(lats), max(lats), min(lons), max(lons))

    def index_at(self, t):
        f = max(0.0, min(1.0, t / self.duration))
        i = bisect.bisect_left(self.fracs, f)
        return max(0, min(len(self.pts) - 1, i))

    def values(self, t):
        i = self.index_at(t)
        return {"i": i, "speed": self.speed[i], "alt": self.pts[i][2], "dist": self.cum[i],
                "utc": self.utcs[i] if i < len(self.utcs) else None}


class OverlayPainter:
    """Dibuja la franja inferior con los datos para un instante dado."""

    def __init__(self, tl, width, height, opts, lang, start_local=None):
        self.tl, self.w, self.h, self.opts, self.lang = tl, width, height, opts, lang
        self.u = height / 300.0                   # unidad: la franja mide 300 px a 1080p
        self.accent = QColor(opts.get("accent") or "#00f2fe")
        self.start_local = start_local
        self.mph = opts.get("units") == "mph"
        self._route = None
        self.font = QFont("Century Gothic")

    def _map_rect(self):
        s = self.h - 24 * self.u
        return QRectF(self.w - s - 24 * self.u, 12 * self.u, s, s)

    def _route_path(self, r):
        if self._route is None:
            la0, la1, lo0, lo1 = self.tl.box
            midlat = math.radians((la0 + la1) / 2)
            span_x = max(1e-6, (lo1 - lo0) * math.cos(midlat))
            span_y = max(1e-6, la1 - la0)
            pad = 14 * self.u
            sc = min((r.width() - 2 * pad) / span_x, (r.height() - 2 * pad) / span_y)
            ox = r.left() + (r.width() - span_x * sc) / 2
            oy = r.top() + (r.height() - span_y * sc) / 2

            def xy(p):
                return QPointF(ox + (p[1] - lo0) * math.cos(midlat) * sc, oy + (la1 - p[0]) * sc)
            self._xy = xy
            path = QPainterPath()
            step = max(1, len(self.tl.pts) // 1500)
            for j, p in enumerate(self.tl.pts[::step]):
                (path.moveTo if j == 0 else path.lineTo)(xy(p))
            self._route = path
        return self._route

    def paint(self, t):
        img = QImage(self.w, self.h, QImage.Format.Format_RGBA8888)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        v = self.tl.values(t)
        u = self.u
        x = 28 * u
        # --- velocidad: arco + numero grande
        if self.opts.get("speed"):
            speed = v["speed"] / 1.609344 if self.mph else v["speed"]
            vmax = max(10.0, (self.tl.vmax / 1.609344 if self.mph else self.tl.vmax))
            c = QPointF(x + 120 * u, self.h - 120 * u)
            rad = 100 * u
            rect = QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawEllipse(c, rad + 10 * u, rad + 10 * u)
            pen = QPen(QColor(255, 255, 255, 60), 12 * u, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, 225 * 16, -270 * 16)
            pen.setColor(self.accent)
            p.setPen(pen)
            frac = max(0.0, min(1.0, speed / vmax))
            if frac > 0.004:
                p.drawArc(rect, 225 * 16, int(-270 * 16 * frac))
            self._text(p, QRectF(c.x() - rad, c.y() - 50 * u, 2 * rad, 80 * u), f"{speed:.0f}", 64 * u, True,
                       QColor("#ffffff"))
            self._text(p, QRectF(c.x() - rad, c.y() + 26 * u, 2 * rad, 30 * u), "mph" if self.mph else "km/h",
                       20 * u, False, QColor(255, 255, 255, 190))
            x += 260 * u
        # --- columna de datos
        rows = []
        if self.opts.get("alt"):
            alt = v["alt"] * 3.28084 if self.mph else v["alt"]
            rows.append((tr(self.lang, "ov_alt"), f"{alt:.0f} {'ft' if self.mph else 'm'}"))
        if self.opts.get("dist"):
            d = v["dist"]
            if self.mph:
                txt = f"{d / 1609.344:.2f} mi"
            else:
                txt = f"{d / 1000:.2f} km" if d >= 1000 else f"{d:.0f} m"
            rows.append((tr(self.lang, "ov_dist"), txt))
        if self.opts.get("clock"):
            when = None
            if v["utc"]:
                when = datetime.fromtimestamp(v["utc"])
            elif self.start_local:
                when = datetime.fromtimestamp(self.start_local.timestamp() + t)
            if when:
                rows.append((tr(self.lang, "ov_time"), when.strftime("%H:%M:%S")))
        if rows:
            box_h = (len(rows) * 62 + 20) * u
            box = QRectF(x, self.h - box_h - 20 * u, 250 * u, box_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(box, 18 * u, 18 * u)
            for k, (label, val) in enumerate(rows):
                y = box.top() + 12 * u + k * 62 * u
                self._text(p, QRectF(box.left() + 20 * u, y, box.width() - 30 * u, 22 * u), label.upper(), 15 * u,
                           True, QColor(255, 255, 255, 150), Qt.AlignmentFlag.AlignLeft)
                self._text(p, QRectF(box.left() + 20 * u, y + 20 * u, box.width() - 30 * u, 40 * u), val, 32 * u,
                           True, QColor("#ffffff"), Qt.AlignmentFlag.AlignLeft)
        # --- minimapa
        if self.opts.get("map") and len(self.tl.pts) > 1:
            r = self._map_rect()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(r, 18 * u, 18 * u)
            route = self._route_path(r)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 110), 5 * u, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawPath(route)
            # tramo ya recorrido en el color de la app
            done = QPainterPath()
            step = max(1, len(self.tl.pts) // 1500)
            pts = self.tl.pts[:v["i"] + 1:step] + [self.tl.pts[v["i"]]]
            for j, q in enumerate(pts):
                (done.moveTo if j == 0 else done.lineTo)(self._xy(q))
            p.setPen(QPen(self.accent, 6 * u, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(done)
            p.setPen(QPen(QColor("#0d0d12"), 3 * u))
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(self._xy(self.tl.pts[v["i"]]), 9 * u, 9 * u)
        p.end()
        return img

    def _text(self, p, rect, text, px, bold, color, align=Qt.AlignmentFlag.AlignHCenter):
        f = QFont(self.font)
        f.setPixelSize(max(6, int(px)))
        f.setBold(bold)
        p.setFont(f)
        # sombra suave para que se lea sobre fondos claros
        p.setPen(QColor(0, 0, 0, 160))
        p.drawText(rect.translated(1.5 * self.u, 1.5 * self.u), align | Qt.AlignmentFlag.AlignVCenter, text)
        p.setPen(color)
        p.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, text)


def _even(x):
    return int(x) // 2 * 2


# Codificadores H.264 por hardware (placa de video NVIDIA / Intel / AMD):
# varias veces mas rapidos que x264. Se prueba cada uno con un video minimo
# y se usa el primero que funcione; si ninguno, x264.
_ENCODERS = (
    ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"]),
    ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21", "-pix_fmt", "nv12"]),
    ("h264_amf", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "21", "-qp_p", "21",
                  "-pix_fmt", "yuv420p"]),
)
_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
_ENCODER_CHOICE = {}


def video_encoder_args():
    if "args" not in _ENCODER_CHOICE:
        _ENCODER_CHOICE["args"] = _X264
        exe = ffmpeg_exe()
        for name, args in _ENCODERS:
            try:
                r = subprocess.run([exe, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=0.3"]
                                   + args + ["-f", "null", "-"], capture_output=True, timeout=20,
                                   startupinfo=_hidden_startupinfo())
            except Exception:
                continue
            if r.returncode == 0:
                _ENCODER_CHOICE["args"] = args
                break
        print("[Video con datos] codificador:", _ENCODER_CHOICE["args"][1])
    return list(_ENCODER_CHOICE["args"])


def overlay_job(src, opts, lang, out_dir=None):
    """Video nuevo con los datos de la telemetria encima. El original no se toca."""
    opts = dict(DEFAULT_OPTS, **(opts or {}))
    info = read_mp4_info(src)
    duration = info.get("duration") or 0
    v = info.get("video") or {}

    def run(progress, cancel):
        track = read_gopro_track(src)
        if not track or not track.get("points"):
            return False, tr(lang, "ov_no_gps")
        if not duration:
            return False, tr(lang, "ov_no_gps")
        src_w, src_h = v.get("w") or 1920, v.get("h") or 1080
        out_h = min(src_h, int(opts.get("height") or src_h))
        out_w = _even(src_w * out_h / src_h)
        out_h = _even(out_h)
        strip_h = _even(out_h * 0.28)
        tl = TelemetryTimeline(track, duration)
        from .media import _read_mp4_creation_time
        painter = OverlayPainter(tl, out_w, strip_h, opts, lang, _read_mp4_creation_time(src))
        d = out_dir or share_dir()
        os.makedirs(d, exist_ok=True)
        base = os.path.splitext(os.path.basename(src))[0]
        out = _unique_path(d, f"{base}_datos", ".mp4")
        part = out + ".part.mp4"
        exe = ffmpeg_exe()
        if not exe:
            raise RuntimeError("ffmpeg")
        scale = f"scale={out_w}:{out_h}," if out_h != src_h else ""
        cmd = [exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               "-i", src,
               "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{out_w}x{strip_h}", "-r", str(OVERLAY_FPS), "-i", "pipe:0",
               "-filter_complex", f"[0:v]{scale}format=yuv420p[b];[b][1:v]overlay=0:H-h:eof_action=repeat:format=auto[v]",
               "-map", "[v]", "-map", "0:a:0?"] + video_encoder_args() + [
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{duration:.3f}", part]
        errlog = tempfile.NamedTemporaryFile(prefix="gpsync_ov_", suffix=".log", delete=False)
        errlog.close()
        ferr = open(errlog.name, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=ferr,
                                startupinfo=_hidden_startupinfo())
        state = {"enc": 0.0, "alive": True}

        def read_progress():
            for raw in proc.stdout:
                line = raw.decode("ascii", "ignore")
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        state["enc"] = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        pass
            state["alive"] = False
        threading.Thread(target=read_progress, daemon=True).start()
        frames = int(math.ceil(duration * OVERLAY_FPS)) + 1
        cancelled = False
        try:
            for k in range(frames):
                if cancel():
                    cancelled = True
                    break
                img = painter.paint(k / OVERLAY_FPS)
                try:
                    proc.stdin.write(bytes(img.constBits()))
                except (BrokenPipeError, OSError):
                    break
                if k % 5 == 0:
                    progress(min(99.0, state["enc"] / duration * 100.0))
            try:
                proc.stdin.close()
            except OSError:
                pass
            while proc.poll() is None:
                if cancel():
                    cancelled = True
                    proc.kill()
                    break
                progress(min(99.0, state["enc"] / duration * 100.0))
                time.sleep(0.3)
            proc.wait()
        except BaseException:
            proc.kill()
            raise
        finally:
            ferr.close()
        try:
            err = open(errlog.name, encoding="utf-8", errors="replace").read().strip()
            os.remove(errlog.name)
        except Exception:
            err = ""
        if cancelled or cancel():
            if cancelled:
                proc.kill()
            try:
                os.remove(part)
            except OSError:
                pass
            return False, "cancelled"
        if proc.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
            try:
                os.remove(part)
            except OSError:
                pass
            return False, tr(lang, "ffmpeg_failed", e=(err.splitlines()[-1] if err else f"codigo {proc.returncode}"))
        os.replace(part, out)
        try:
            shutil.copystat(src, out)
            _copy_creation_time(src, out)
        except Exception:
            pass
        return True, {"output": out, "note": tr(lang, "ov_done", name=os.path.basename(out),
                                               size=_human_size(os.path.getsize(out)))}
    return {"name": os.path.basename(src), "size": max(1, int(duration * 1000)), "run": run}
