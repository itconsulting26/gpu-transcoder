"""Boucle du worker : scans (SSHFS + ffprobe) et transcodages (NVENC), état GPU."""
import os
import signal
import subprocess
import threading
import time
import traceback

from django.db import close_old_connections
from django.utils import timezone

from .models import MEDIA_EXTS, Config, Job, MediaFile, ScanTask, WorkerStatus
from .probe import probe_into
from .ssh import ensure_mounted, unmount_all
from .transcode import process_job

_stop = threading.Event()
_running = {}  # job_id -> Thread
_lock = threading.Lock()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ GPU

def gpu_status():
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,utilization.encoder,utilization.decoder,"
             "memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"message": r.stderr.strip()[:300]}
        p = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        f = lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0.0  # noqa: E731
        return {"gpu_name": p[0], "gpu_util": f(p[1]), "gpu_enc": f(p[2]), "gpu_dec": f(p[3]),
                "gpu_mem_used": int(f(p[4])), "gpu_mem_total": int(f(p[5])), "gpu_temp": f(p[6]),
                "gpu_power": f(p[7]), "message": ""}
    except Exception as e:  # noqa: BLE001
        return {"message": f"nvidia-smi indisponible : {e}"[:300]}


def heartbeat_loop():
    while not _stop.is_set():
        try:
            close_old_connections()
            WorkerStatus.objects.update_or_create(pk=1, defaults={"alive": True, **gpu_status()})
        except Exception as e:  # noqa: BLE001
            log(f"heartbeat : {e}")
        _stop.wait(5)


# ------------------------------------------------------------------ transcodage

def _reap():
    with _lock:
        for jid, t in list(_running.items()):
            if not t.is_alive():
                del _running[jid]


def transcode_loop():
    # au démarrage : jobs restés "running" (worker redémarré) → remis en file
    Job.objects.filter(status__in=("running", "cancelling")).update(status="queued", phase="wait", pid=None)
    while not _stop.is_set():
        try:
            close_old_connections()
            _reap()
            cfg = Config.get()
            if not cfg.paused:
                with _lock:
                    free = cfg.concurrency - len(_running)
                if free > 0:
                    jobs = (Job.objects.filter(status="queued")
                            .exclude(pk__in=list(_running.keys()))
                            .order_by("-priority", "created")[:free])
                    for job in jobs:
                        log(f"Démarrage job #{job.pk} : {job.file.path}")
                        t = threading.Thread(target=process_job, args=(job.pk, log), daemon=True)
                        with _lock:
                            _running[job.pk] = t
                        t.start()
            # annulation d'un job encore en attente
            Job.objects.filter(status="cancelling", pid__isnull=True, phase="wait").update(
                status="cancelled", finished=timezone.now())
        except Exception:  # noqa: BLE001
            log("transcode_loop :\n" + traceback.format_exc())
        _stop.wait(2)


# ------------------------------------------------------------------ scan

def run_scan(task):
    server = task.server
    cfg = Config.get()
    ScanTask.objects.filter(pk=task.pk).update(status="running", started=timezone.now())
    try:
        ensure_mounted(server)
        root = server.local_path(task.path)
        if not os.path.isdir(root):
            raise RuntimeError(f"Répertoire introuvable : {task.path}")
        seen = probed = flagged = 0
        walker = os.walk(root) if task.recursive else [(root, [], [e.name for e in os.scandir(root) if e.is_file()])]
        for dirpath, dirnames, filenames in walker:
            dirnames.sort()
            if _stop.is_set() or ScanTask.objects.filter(pk=task.pk, status="cancelled").exists():
                return
            for fn in sorted(filenames):
                if os.path.splitext(fn)[1].lower() not in MEDIA_EXTS or fn.startswith("."):
                    continue
                local = os.path.join(dirpath, fn)
                remote = os.path.normpath(os.path.join(server.base_path, os.path.relpath(local, server.mount_point)))
                seen += 1
                try:
                    st = os.stat(local)
                except OSError:
                    continue
                mf, created = MediaFile.objects.get_or_create(server=server, path=remote, defaults={"name": fn})
                if mf.status in ("running", "queued"):
                    continue
                fresh = (not created and mf.probed_at and mf.size == st.st_size and abs(mf.mtime - st.st_mtime) < 1
                         and not mf.probe_error)
                if not fresh:
                    ScanTask.objects.filter(pk=task.pk).update(current=remote, files_seen=seen, files_probed=probed, files_flagged=flagged)
                    probe_into(mf, local, cfg)
                    probed += 1
                    if mf.status in ("done", "nogain", "error") and (mf.size != st.st_size):
                        mf.status = "new"
                else:
                    mf.apply_detection(cfg)
                mf.name = fn
                mf.save()
                if mf.needs_transcode and mf.status == "new":
                    flagged += 1
            ScanTask.objects.filter(pk=task.pk).update(files_seen=seen, files_probed=probed, files_flagged=flagged)
        ScanTask.objects.filter(pk=task.pk).update(status="done", finished=timezone.now(), current="",
                                                   files_seen=seen, files_probed=probed, files_flagged=flagged)
        log(f"Scan #{task.pk} terminé : {seen} fichiers, {probed} analysés, {flagged} à transcoder")
    except Exception as e:  # noqa: BLE001
        log(f"Scan #{task.pk} erreur : {e}")
        ScanTask.objects.filter(pk=task.pk).update(status="error", finished=timezone.now(), error=str(e)[:4000])


def scan_loop():
    ScanTask.objects.filter(status="running").update(status="queued")
    while not _stop.is_set():
        try:
            close_old_connections()
            task = ScanTask.objects.filter(status="queued").select_related("server").order_by("created").first()
            if task:
                run_scan(task)
        except Exception:  # noqa: BLE001
            log("scan_loop :\n" + traceback.format_exc())
        _stop.wait(3)


# ------------------------------------------------------------------ main

def main():
    log("Worker démarré")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    os.makedirs("/mnt/src", exist_ok=True)
    threads = [threading.Thread(target=f, daemon=True, name=n) for f, n in
               ((heartbeat_loop, "heartbeat"), (transcode_loop, "transcode"), (scan_loop, "scan"))]
    for t in threads:
        t.start()
    while not _stop.is_set():
        _stop.wait(1)
    log("Arrêt demandé : attente des jobs en cours (max 30 s)…")
    deadline = time.time() + 30
    while time.time() < deadline and any(t.is_alive() for t in _running.values()):
        time.sleep(1)
    try:
        WorkerStatus.objects.filter(pk=1).update(alive=False)
        unmount_all()
    except Exception:  # noqa: BLE001
        pass
    log("Worker arrêté")
