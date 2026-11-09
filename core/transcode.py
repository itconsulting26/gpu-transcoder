"""Transcodage NVENC d'un Job avec progression en temps réel et remplacement sûr de l'original."""
import os
import shutil
import subprocess
import time

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .models import Config, Job, SUPPORTED_CODECS
from .probe import ffprobe, probe_into, summarize
from .ssh import ensure_mounted


class JobCancelled(Exception):
    pass


class JobFailed(Exception):
    pass


def _set(job, **fields):
    """Met à jour quelques champs du job en base (sans écraser le reste)."""
    Job.objects.filter(pk=job.pk).update(**fields)
    for k, v in fields.items():
        setattr(job, k, v)


def _log(job, text):
    job.append_log(text)
    Job.objects.filter(pk=job.pk).update(log=job.log)


def _cancel_requested(job):
    return Job.objects.filter(pk=job.pk, status="cancelling").exists()


def tmp_path_for(local_path):
    d, n = os.path.split(local_path)
    stem, ext = os.path.splitext(n)
    return os.path.join(d, f".{stem}.gpu-tmp{ext}")


def build_cmd(mf, src, dst, cfg, hw_decode=True):
    """Construit la commande ffmpeg. Même codec qu'à l'origine (H.264→H.264, HEVC→HEVC)."""
    codec = mf.vcodec
    if codec == "h264":
        enc = "h264_nvenc"
    elif codec == "hevc":
        enc = "hevc_nvenc"
    else:
        # formats hors H.264/HEVC : transcodés en H.264 uniquement à la demande (force=True)
        enc = "h264_nvenc"
    ten_bit = mf.is_10bit
    # H.264 10 bits n'existe pas en NVENC → décodage CPU + conversion 8 bits
    if enc == "h264_nvenc" and ten_bit:
        hw_decode = False

    # --- débit cible : fraction du débit vidéo source, borné [min, max]
    target = target_bitrate(mf, cfg)
    maxrate = min(cfg.max_bitrate_kbps, int(target * 1.25))

    cmd = [settings.FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1"]
    if hw_decode:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    # vidéo (hors images jointes) + audio + data ; les sous-titres/pièces jointes sont remis au remux
    # (ffmpeg 6.1 s'effondre à ~1x quand il copie des sous-titres pendant un encodage)
    cmd += ["-i", src, "-map", "0:V", "-map", "0:a?", "-map", "0:d?", "-c", "copy", "-c:v", enc,
            "-preset", cfg.preset, "-rc", "vbr",
            "-b:v", f"{target}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k"]
    if cfg.cq:
        cmd += ["-cq", str(cfg.cq)]
    if not cfg.fast_mode:
        cmd += ["-tune", "hq", "-spatial-aq", "1", "-aq-strength", "8"]
    if enc == "hevc_nvenc":
        cmd += ["-profile:v", "main10" if ten_bit else "main", "-tag:v", "hvc1"] if mf.ext in (".mp4", ".mov", ".m4v") else ["-profile:v", "main10" if ten_bit else "main"]
    elif enc == "h264_nvenc":
        if ten_bit:
            cmd += ["-pix_fmt", "yuv420p"]
        cmd += ["-profile:v", "high"]
    # audio : copie ou réencodage piste par piste (métadonnées et dispositions conservées par ffmpeg)
    for i, (codec, kbps, ch) in enumerate(audio_plan(mf, cfg)):
        if codec == "copy":
            cmd += [f"-c:a:{i}", "copy"]
        else:
            cmd += [f"-c:a:{i}", codec, f"-b:a:{i}", f"{kbps}k", f"-ac:a:{i}", str(ch)]
    cmd += ["-max_muxing_queue_size", "4096", "-map_metadata", "0", "-map_chapters", "0"]
    cmd += ["-f", _container_for(dst), dst]
    return cmd


def target_bitrate(mf, cfg):
    """Débit vidéo cible (kb/s) : fraction du débit vidéo source, borné [min, max]."""
    src_v = mf.vbitrate_kbps or (int(mf.bitrate_kbps * 0.85) if mf.bitrate_kbps else 0)
    if cfg.size_ratio and cfg.size_ratio > 0 and src_v:
        target = int(src_v * cfg.size_ratio)
    else:
        target = cfg.target_bitrate_kbps
    return max(cfg.min_bitrate_kbps, min(cfg.max_bitrate_kbps, target))


LOSSLESS_AUDIO = {"truehd", "flac", "alac", "mlp", "pcm_s16le", "pcm_s24le", "pcm_s32le", "dts"}  # dts : souvent DTS-HD


def audio_plan(mf, cfg):
    """Pour chaque piste audio : (codec de sortie ou 'copy', débit kb/s prévu, canaux)."""
    plan = []
    for a in (mf.audio_info or []):
        ch = a.get("channels") or 2
        src = a.get("kbps") or 0
        if ch <= 2:
            codec, target = "aac", cfg.audio_stereo_kbps
        else:
            codec, target = "eac3", cfg.audio_surround_kbps
            ch = min(ch, 6)
        if not cfg.audio_reencode:
            plan.append(("copy", src, ch))
        elif a.get("codec") in LOSSLESS_AUDIO or src == 0 or src > target * 1.15:
            plan.append((codec, target, ch))
        else:
            plan.append(("copy", src, ch))
    return plan


def estimate_output(mf, cfg):
    """Taille de sortie estimée (octets) et gain attendu (%)."""
    audio_kbps = sum(k for _, k, _ in audio_plan(mf, cfg)) if mf.audio_info else mf.abitrate_kbps
    kbps = target_bitrate(mf, cfg) + audio_kbps + 20  # +20 kb/s : conteneur, sous-titres
    est = int(kbps * 1000 / 8 * mf.duration)
    gain = 100 * (1 - est / mf.size) if mf.size else 0
    return est, gain


def _container_for(path):
    ext = os.path.splitext(path)[1].lower()
    return {".mkv": "matroska", ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".ts": "mpegts", ".m2ts": "mpegts",
            ".webm": "matroska", ".avi": "avi", ".wmv": "asf", ".flv": "flv", ".mpg": "mpeg", ".mpeg": "mpeg"}.get(ext, "matroska")


def build_remux_cmd(step1, src, dst):
    """Copie pure : flux encodés (step1 local) + sous-titres / pièces jointes / métadonnées de l'original → dst."""
    cmd = [settings.FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
           "-i", step1, "-i", src, "-map", "0", "-map", "1:s?", "-map", "1:t?", "-c", "copy",
           "-map_metadata", "1", "-map_chapters", "1", "-max_muxing_queue_size", "4096"]
    if os.path.splitext(dst)[1].lower() in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", _container_for(dst), dst]
    return cmd


LOCAL_TMP = "/data/tmp"


def _parse_kv(line):
    if "=" in line:
        k, v = line.split("=", 1)
        return k.strip(), v.strip()
    return None, None


def run_ffmpeg(job, cmd, duration):
    """Lance ffmpeg, suit la progression, met à jour le job toutes les ~2 s. Retourne (code, stderr_tail)."""
    _set(job, ffmpeg_cmd=(job.ffmpeg_cmd + "\n\n" if job.ffmpeg_cmd else "") + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    _set(job, pid=proc.pid)
    last_update = 0
    state = {}
    try:
        for line in proc.stdout:
            k, v = _parse_kv(line)
            if k is None:
                continue
            state[k] = v
            if k != "progress":
                continue
            now = time.time()
            if now - last_update < 2 and v != "end":
                continue
            last_update = now
            try:
                out_us = int(state.get("out_time_us") or state.get("out_time_ms") or 0)
            except ValueError:
                out_us = 0
            out_s = out_us / 1e6
            speed_s = state.get("speed", "0x").rstrip("x")
            try:
                speed = float(speed_s) if speed_s not in ("N/A", "") else 0.0
            except ValueError:
                speed = 0.0
            fps = _f(state.get("fps"))
            bitrate = _f(state.get("bitrate", "0kbits/s").replace("kbits/s", ""))
            total_size = int(state.get("total_size") or 0) if str(state.get("total_size", "0")).isdigit() else 0
            frame = int(state.get("frame") or 0) if str(state.get("frame", "0")).isdigit() else 0
            percent = min(100.0, 100.0 * out_s / duration) if duration else 0
            eta = (duration - out_s) / speed if (speed > 0 and duration) else None
            _set(job, percent=round(percent, 2), fps=fps, speed=speed, out_time_s=out_s,
                 out_bitrate_kbps=bitrate, out_size=total_size, eta_s=eta, frame=frame)
            if _cancel_requested(job):
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise JobCancelled()
        code = proc.wait()
        err = proc.stderr.read()[-4000:]
        return code, err
    finally:
        if proc.poll() is None:
            proc.kill()
        _set(job, pid=None)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def verify(job, src, dst, src_info, cfg):
    """Vérifie que la sortie est saine avant tout remplacement. Lève JobFailed sinon."""
    if not os.path.exists(dst) or os.path.getsize(dst) < 1024:
        raise JobFailed("Fichier de sortie absent ou vide")
    try:
        out = summarize(ffprobe(dst))
    except Exception as e:  # noqa: BLE001
        raise JobFailed(f"La sortie n'est pas lisible par ffprobe : {e}")
    tol = cfg.verify_duration_tolerance_s
    if abs(out["duration"] - src_info["duration"]) > tol:
        raise JobFailed(f"Durée différente : original {src_info['duration']:.2f}s, sortie {out['duration']:.2f}s (tolérance {tol}s)")
    for key, label in (("n_video", "vidéo"), ("n_audio", "audio"), ("n_sub", "sous-titres")):
        if out[key] != src_info[key]:
            raise JobFailed(f"Nombre de flux {label} différent : {src_info[key]} → {out[key]}")
    if out["vcodec"] != src_info["vcodec"] and src_info["vcodec"] in SUPPORTED_CODECS:
        raise JobFailed(f"Codec de sortie inattendu : {out['vcodec']}")
    # décodage rapide des 5 dernières secondes pour s'assurer que le fichier est complet
    r = subprocess.run([settings.FFMPEG, "-v", "error", "-sseof", "-5", "-i", dst, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or r.stderr.strip():
        raise JobFailed(f"Erreur au décodage de la fin du fichier : {r.stderr.strip()[-500:]}")
    return out


def replace_original(src, dst, keep_backup=False):
    """Remplace src par dst en conservant nom, droits, propriétaire et dates. Renommage atomique."""
    st = os.stat(src)
    try:
        os.chmod(dst, st.st_mode & 0o7777)
    except OSError:
        pass
    try:
        os.chown(dst, st.st_uid, st.st_gid)
    except OSError:
        pass
    try:
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    if keep_backup:
        shutil.copy2(src, src + ".bak")
    os.replace(dst, src)


def process_job(job_id, log_print=print):
    close_old_connections()
    job = Job.objects.select_related("file", "file__server").get(pk=job_id)
    mf, server = job.file, job.file.server
    cfg = Config.get()
    local = None
    tmp = None
    step1 = None
    _set(job, status="running", phase="mount", started=timezone.now(), attempt=job.attempt + 1,
         percent=0, fps=0, speed=0, out_time_s=0, out_size=0, eta_s=None, error="", ffmpeg_cmd="")
    mf.status = "running"
    mf.save(update_fields=["status"])
    try:
        # 1. montage
        ensure_mounted(server, log=lambda t: _log(job, t))
        local = server.local_path(mf.path)
        if not os.path.isfile(local):
            raise JobFailed(f"Fichier introuvable via le montage : {local}")
        # 2. analyse fraîche
        _set(job, phase="probe")
        probe_into(mf, local, cfg)
        mf.save()
        if mf.probe_error:
            raise JobFailed(f"ffprobe : {mf.probe_error}")
        if not mf.supported and not job.force:
            raise JobFailed(f"Codec {mf.vcodec or 'inconnu'} hors H.264/HEVC : à transcoder uniquement à la demande (forcer)")
        src_info = summarize(ffprobe(local))
        est, gain = estimate_output(mf, cfg)
        plan = ", ".join(f"{a.get('lang') or '?'} {a.get('codec')} {a.get('channels')}ch {a.get('kbps')}k->{c} {k}k"
                         for a, (c, k, _) in zip(mf.audio_info or [], audio_plan(mf, cfg)))
        _log(job, f"[estimation] vidéo source ~{mf.vbitrate_kbps} kb/s -> cible {target_bitrate(mf, cfg)} kb/s ; "
                  f"audio : {plan or 'aucun'} ; sortie ~{est // 1_000_000} Mo soit {gain:.0f} % de gain\n")
        if gain < cfg.min_gain_pct:
            _set(job, status="nogain", phase="end", finished=timezone.now(), size_before=mf.size,
                 error=f"Gain estimé {gain:.0f} % < {cfg.min_gain_pct} % : non encodé (audio = {100 * mf.abitrate_kbps / max(mf.bitrate_kbps, 1):.0f} % du débit)")
            mf.status = "nogain"
            mf.save(update_fields=["status"])
            return
        _set(job, size_before=mf.size, target_codec=("h264_nvenc" if mf.vcodec == "h264" else "hevc_nvenc" if mf.vcodec == "hevc" else "h264_nvenc"))
        # 3. encodage local (tentative GPU-décodage puis repli décodage CPU)
        _set(job, phase="encode")
        os.makedirs(LOCAL_TMP, exist_ok=True)
        step1 = os.path.join(LOCAL_TMP, f"job{job.pk}{mf.ext}")
        need = int(mf.size * 0.8) + 200 * 1024 * 1024
        free = shutil.disk_usage(LOCAL_TMP).free
        if free < need:
            raise JobFailed(f"Espace local insuffisant dans {LOCAL_TMP} : {free // 1e6} Mo libres, {need // 1e6} Mo requis")
        tmp = tmp_path_for(local)
        for pth in (tmp, step1):
            if os.path.exists(pth):
                os.remove(pth)
        code, err = None, ""
        for hw in (True, False):
            if not hw and not job.hw_decode:
                break
            cmd = build_cmd(mf, local, step1, cfg, hw_decode=hw)
            _set(job, hw_decode=hw)
            _log(job, f"[ffmpeg] décodage {'GPU' if hw else 'CPU'} : {' '.join(cmd)}\n")
            code, err = run_ffmpeg(job, cmd, mf.duration)
            if code == 0:
                break
            _log(job, f"[ffmpeg] code {code}\n{err}\n")
            if hw and os.path.exists(step1):
                os.remove(step1)
                _log(job, "[ffmpeg] nouvel essai avec décodage CPU\n")
        if code != 0:
            raise JobFailed(f"ffmpeg a échoué (code {code}) : {err.strip()[-800:]}")
        # 3b. remux vers le serveur : + sous-titres, pièces jointes, chapitres, métadonnées
        _set(job, phase="remux", percent=0)
        rcmd = build_remux_cmd(step1, local, tmp)
        _log(job, f"[remux] {' '.join(rcmd)}\n")
        code, err = run_ffmpeg(job, rcmd, mf.duration)
        if code != 0:
            raise JobFailed(f"remux a échoué (code {code}) : {err.strip()[-800:]}")
        os.remove(step1)
        step1 = None
        # 4. vérification
        _set(job, phase="verify", percent=100)
        out = verify(job, local, tmp, src_info, cfg)
        size_after = os.path.getsize(tmp)
        if size_after >= mf.size:
            os.remove(tmp)
            _log(job, f"[info] sortie ({size_after}) pas plus petite que l'original ({mf.size}) : original conservé\n")
            _set(job, status="nogain", phase="end", finished=timezone.now(), size_after=size_after)
            mf.status = "nogain"
            mf.save(update_fields=["status"])
            return
        # 5. remplacement atomique
        _set(job, phase="replace")
        replace_original(local, tmp, keep_backup=cfg.keep_backup)
        tmp = None
        # contrôle final sur le fichier remplacé
        final = summarize(ffprobe(local))
        if abs(final["duration"] - src_info["duration"]) > cfg.verify_duration_tolerance_s:
            raise JobFailed("Contrôle final : durée incohérente après remplacement")
        mf.size, mf.size_after = size_after, size_after
        mf.bitrate_kbps, mf.vbitrate_kbps = final["bitrate_kbps"], final["vbitrate_kbps"]
        mf.mtime = os.stat(local).st_mtime
        mf.status = "done"
        mf.needs_transcode = False
        mf.save()
        _set(job, status="done", phase="end", finished=timezone.now(), size_after=size_after)
        _log(job, f"[ok] {job.size_before} → {size_after} octets\n")
        log_print(f"Job #{job.pk} terminé : {mf.name} ({job.size_before} → {size_after})")
    except JobCancelled:
        _set(job, status="cancelled", phase="end", finished=timezone.now())
        mf.status = "new"
        mf.save(update_fields=["status"])
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}" if not isinstance(e, JobFailed) else str(e)
        _log(job, f"[erreur] {msg}\n")
        _set(job, status="error", phase="end", finished=timezone.now(), error=msg[:4000])
        mf.status = "error"
        mf.save(update_fields=["status"])
        log_print(f"Job #{job.pk} en erreur : {msg}")
    finally:
        for pth in (tmp, step1):
            if pth and os.path.exists(pth):
                try:
                    os.remove(pth)
                except OSError:
                    pass
        close_old_connections()
