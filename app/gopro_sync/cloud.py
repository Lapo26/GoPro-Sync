"""Respaldo en la nube (Google Drive, OneDrive, Dropbox, un NAS por SFTP...)
con rclone (https://rclone.org), que se instala aparte y se configura una
vez con "rclone config". Se usa "rclone copy": sube lo nuevo o cambiado y
NUNCA borra nada en la nube."""
import json
import os
import shutil
import subprocess
import time

from .env import IS_WINDOWS, _hidden_startupinfo


def find_rclone(configured=""):
    if configured and os.path.isfile(configured):
        return configured
    exe = shutil.which("rclone")
    if exe:
        return exe
    if IS_WINDOWS:
        home = os.path.expanduser("~")
        cands = [os.path.join(home, "scoop", "shims", "rclone.exe"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "rclone.exe"),
                 r"C:\Program Files\rclone\rclone.exe", r"C:\rclone\rclone.exe"]
        for c in cands:
            if os.path.isfile(c):
                return c
    return ""


def list_remotes(exe):
    try:
        out = subprocess.run([exe, "listremotes"], capture_output=True, text=True, timeout=20,
                             startupinfo=_hidden_startupinfo()).stdout
        return [x.strip().rstrip(":") for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def _run_json(cmd, total_hint, progress, cancel):
    """Corre rclone con logs JSON y avisa el avance (bytes subidos / total)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", startupinfo=_hidden_startupinfo(), bufsize=1)
    last_err = ""
    last_stats = {}
    try:
        for line in proc.stdout:
            if cancel():
                proc.kill()
                return False, "cancelled", {}
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            st = msg.get("stats")
            if st:
                total = st.get("totalBytes") or total_hint or 0
                if total:
                    progress(min(99.0, st.get("bytes", 0) * 100.0 / total))
                last_stats = st
            if msg.get("level") == "error":
                last_err = msg.get("msg", "")
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    return proc.returncode == 0, last_err, last_stats


def cloud_backup_job(exe, source, remote, folder, verify, lang):
    """Sube la biblioteca a remote:folder (solo lo nuevo; nunca borra)."""
    from .i18n import _human_size, tr
    dest = f"{remote}:{folder.strip('/')}" if folder else f"{remote}:"

    def run(progress, cancel):
        if not exe or not os.path.isfile(exe):
            return False, tr(lang, "cloud_no_rclone")
        cmd = [exe, "copy", source, dest, "--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE",
               "-v", "--exclude", "*.part", "--exclude", "*.part.mp4", "--transfers", "4"]
        ok, err, st = _run_json(cmd, 0, lambda p: progress(p * (0.85 if verify else 1.0)), cancel)
        if err == "cancelled":
            return False, "cancelled"
        if not ok:
            return False, tr(lang, "cloud_failed", e=err or "rclone")
        note = tr(lang, "cloud_done", n=st.get("transfers", 0), size=_human_size(st.get("bytes", 0)), dest=dest)
        if verify:
            chk = subprocess.run([exe, "check", source, dest, "--one-way", "--size-only", "--exclude", "*.part"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 startupinfo=_hidden_startupinfo())
            progress(100.0)
            if chk.returncode != 0:
                return False, tr(lang, "cloud_verify_failed")
            note += " " + tr(lang, "cloud_verified")
        return True, {"note": note}
    return {"name": dest, "size": 1, "run": run}
