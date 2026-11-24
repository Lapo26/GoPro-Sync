"""Busqueda de versiones nuevas en GitHub y actualizacion."""
import json
import os
import re
import subprocess
import tempfile
from PySide6.QtCore import QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout
import sys
from .version import APP_VERSION, GITHUB_REPO, UPDATE_SIGNER_SUBJECT
from .env import IS_MAC, IS_WINDOWS, POWERSHELL_EXE, _hidden_startupinfo
from .i18n import _human_size, tr
from .ui.widgets import show_toast
from .ui.theme import dialog_css

def _version_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])
    except Exception:
        return (0,)


def _http_get(url, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": f"GoProSync/{APP_VERSION}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


class UpdateCheckThread(QThread):
    """Consulta la ultima version publicada en GitHub (en segundo plano)."""
    found = Signal(dict)          # {"version", "url", "size", "page"} si hay una mas nueva

    def run(self):
        try:
            with _http_get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest") as r:
                data = json.loads(r.read().decode("utf-8"))
            tag = (data.get("tag_name") or "").lstrip("vV")
            if _version_tuple(tag) <= _version_tuple(APP_VERSION):
                return
            def wanted(n):
                n = n.lower()
                if IS_WINDOWS:
                    return n.startswith("goprosync-setup") and n.endswith(".exe")
                if IS_MAC:
                    return n.endswith(".dmg")
                return n.endswith(".appimage")
            asset = next((a for a in data.get("assets") or [] if wanted(a.get("name", ""))), None)
            if asset:
                url = asset.get("browser_download_url") or ""
                if not url.startswith(f"https://github.com/{GITHUB_REPO}/releases/download/"):
                    print("[Actualizaciones] URL de descarga inesperada:", url)
                    return
                self.found.emit({"version": tag, "url": url, "size": asset.get("size", 0),
                                 "sha256": _asset_sha256(asset), "page": data.get("html_url", "")})
        except Exception as e:
            print("[Actualizaciones]", e)


def _asset_sha256(asset):
    """SHA-256 que GitHub calcula para cada archivo del Release ("sha256:<hex>"),
    o None si no viene (sin hash no se instala nada en silencio)."""
    m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", asset.get("digest") or "")
    return m.group(1).lower() if m else None


_SIG_SCRIPT = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:GPSYNC_SIG_PATH; "
    "$c = $s.SignerCertificate; "
    "[pscustomobject]@{ status = [string]$s.Status; "
    "subject = $(if ($c) { $c.Subject } else { '' }); "
    "thumb = $(if ($c) { $c.Thumbprint } else { '' }) } | ConvertTo-Json -Compress"
)


def authenticode_info(path):
    """Firma Authenticode de un .exe: {"status": "Valid"/"NotSigned"/..., "subject",
    "thumb"}, o None si no se pudo consultar. La ruta va por variable de
    entorno, nunca dentro del texto del script."""
    if not IS_WINDOWS:
        return None
    try:
        proc = subprocess.run([POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", _SIG_SCRIPT],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                              env=dict(os.environ, GPSYNC_SIG_PATH=os.path.abspath(path)),
                              startupinfo=_hidden_startupinfo())
        data = json.loads((proc.stdout or "").strip() or "null")
        if isinstance(data, dict) and isinstance(data.get("status"), str):
            return {"status": data["status"], "subject": str(data.get("subject") or ""),
                    "thumb": str(data.get("thumb") or "")}
    except Exception as e:
        print("[Actualizaciones] firma:", e)
    return None


def expected_signer():
    """Subject que debe tener la firma del instalador, o "" si no se exige.
    1) el fijado en version.py; 2) el de la app instalada, si esta firmada
    (asi, una vez que se publica firmada, nadie puede colar una version sin
    firma o firmada por otro)."""
    if UPDATE_SIGNER_SUBJECT:
        return UPDATE_SIGNER_SUBJECT
    if IS_WINDOWS and getattr(sys, "frozen", False):
        own = authenticode_info(sys.executable)
        if own and own["status"] == "Valid" and own["subject"]:
            return own["subject"]
    return ""


def check_installer_signature(path, expected):
    """"" si el instalador se puede ejecutar; si no, el motivo."""
    info = authenticode_info(path)
    if info is None:
        return "no se pudo comprobar la firma del instalador" if expected else ""
    if info["status"] not in ("Valid", "NotSigned"):
        return f"la firma del instalador no es valida ({info['status']})"
    if expected:
        if info["status"] != "Valid":
            return "el instalador no esta firmado"
        if info["subject"] != expected:
            return f"el instalador esta firmado por otro ({info['subject']})"
    return ""


class UpdateDownloadThread(QThread):
    progress = Signal(int)
    done = Signal(str, str)       # ruta, error

    def __init__(self, url, dest, sha256, size=0):
        super().__init__()
        self.url, self.dest = url, dest
        self.sha256, self.size = sha256, int(size or 0)
        self._cancel = False

    def run(self):
        import hashlib
        part = self.dest + ".part"
        try:
            if not self.sha256:
                raise RuntimeError("el Release no publica el SHA-256 del instalador")
            h = hashlib.sha256()
            got = 0
            with _http_get(self.url, timeout=30) as r, open(part, "wb") as f:
                if not r.geturl().startswith("https://"):
                    raise RuntimeError("la descarga no usa HTTPS")
                total = self.size or int(r.headers.get("Content-Length") or 0)
                while not self._cancel:
                    buf = r.read(256 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    h.update(buf)
                    got += len(buf)
                    if total:
                        self.progress.emit(min(100, int(got * 100 / total)))
            if self._cancel:
                raise RuntimeError("cancelado")
            if self.size and got != self.size:
                raise RuntimeError(f"tamaño incorrecto ({got} de {self.size} bytes)")
            if h.hexdigest() != self.sha256:
                raise RuntimeError("el instalador descargado no coincide con el SHA-256 del Release")
            # El SHA-256 viene de la misma API que el archivo: protege de una
            # descarga corrupta, no de un Release publicado por otro. La firma
            # Authenticode es lo que identifica a quien lo compilo.
            # (se comprueba ya con el nombre .exe: con ".part" Windows no sabe leer la firma)
            os.replace(part, self.dest)
            problem = check_installer_signature(self.dest, expected_signer())
            if problem:
                try:
                    os.remove(self.dest)
                except OSError:
                    pass
                raise RuntimeError(problem)
            self.done.emit(self.dest, "")
        except Exception as e:
            try:
                os.remove(part)
            except OSError:
                pass
            self.done.emit("", str(e))


class UpdateDialog(QDialog):
    """Ofrece la version nueva; si se acepta, descarga el instalador y lo
    corre en modo silencioso (se cierra la app y se vuelve a abrir sola)."""

    def __init__(self, parent, lang, info, accent):
        super().__init__(parent)
        self.lang, self.info = lang, info
        self.worker = None
        self.setWindowTitle(tr(lang, "update_title"))
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setStyleSheet(dialog_css(accent, f"""
            QProgressBar {{ background: rgba(255,255,255,0.08); border: none; border-radius: 6px; height: 10px; }}
            QProgressBar::chunk {{ background: {accent}; border-radius: 6px; }}
        """))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)
        q = QLabel(tr(lang, "update_question", v=info["version"], cur=APP_VERSION))
        q.setWordWrap(True)
        q.setStyleSheet("font-size: 17px; font-weight: 800;")
        d = QLabel(tr(lang, "update_detail", size=_human_size(info.get("size") or 0)))
        d.setWordWrap(True)
        d.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.hide()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("font-size: 12px; color: rgba(255,255,255,0.65);")
        lay.addWidget(q)
        lay.addWidget(d)
        lay.addWidget(self.bar)
        lay.addWidget(self.lbl_status)
        lay.addSpacing(6)
        row = QHBoxLayout()
        if info.get("page"):
            notes = QPushButton(tr(lang, "update_notes"))
            notes.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(info["page"])))
            row.addWidget(notes)
        row.addStretch()
        self.btn_later = QPushButton(tr(lang, "update_later"))
        self.btn_later.clicked.connect(self.reject)
        self.btn_now = QPushButton(tr(lang, "update_now" if IS_WINDOWS else "update_open_page"))
        self.btn_now.setObjectName("primary")
        self.btn_now.clicked.connect(self._download)
        row.addWidget(self.btn_later)
        row.addWidget(self.btn_now)
        lay.addLayout(row)

    def _download(self):
        if not IS_WINDOWS or not self.info.get("sha256"):
            # macOS / Linux (o un Release sin SHA-256): se baja a mano desde la pagina del Release
            QDesktopServices.openUrl(QUrl(self.info.get("page") or self.info["url"]))
            self.accept()
            return
        self.btn_now.setEnabled(False)
        self.bar.show()
        self.lbl_status.setText(tr(self.lang, "update_downloading", pct=0))
        # Carpeta temporal propia (nombre impredecible) y version saneada para el nombre del archivo.
        version = re.sub(r"[^0-9A-Za-z.\-]", "_", self.info["version"])
        dest = os.path.join(tempfile.mkdtemp(prefix="GoProSync-update-"), f"GoProSync-Setup-{version}.exe")
        self.worker = UpdateDownloadThread(self.info["url"], dest, self.info.get("sha256"), self.info.get("size"))
        self.worker.progress.connect(lambda pct: (self.bar.setValue(pct),
                                                  self.lbl_status.setText(tr(self.lang, "update_downloading", pct=pct))))
        self.worker.done.connect(self._on_downloaded)
        self.worker.start()

    def _on_downloaded(self, path, error):
        if error:
            self.lbl_status.setText(tr(self.lang, "update_failed", e=error))
            self.btn_now.setEnabled(True)
            return
        self.accept()
        try:
            # /SILENT: solo una barrita de progreso; el instalador cierra la app y la vuelve a abrir.
            subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"])
        except Exception as e:
            show_toast(self.parent(), tr(self.lang, "update_failed", e=e))
            return
        main = self.parent()
        if hasattr(main, "quit_app"):
            QTimer.singleShot(300, main.quit_app)

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker._cancel = True
            self.worker.wait(3000)
        super().reject()
