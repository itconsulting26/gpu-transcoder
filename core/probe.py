"""ffprobe : analyse d'un fichier et mise à jour d'un MediaFile."""
import json
import os
import subprocess

from django.conf import settings
from django.utils import timezone


def ffprobe(path, timeout=300):
    cmd = [settings.FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-2000:] or f"ffprobe code {r.returncode}")
    return json.loads(r.stdout)


def summarize(info):
    """Résumé compact d'un résultat ffprobe."""
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) != 1), None)
    duration = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    size = int(fmt.get("size") or 0)
    bitrate = int(fmt.get("bit_rate") or 0) // 1000
    if not bitrate and duration and size:
        bitrate = int(size * 8 / duration / 1000)
    out = {
        "container": (fmt.get("format_name") or "").split(",")[0],
        "duration": duration,
        "size": size,
        "bitrate_kbps": bitrate,
        "vcodec": (v or {}).get("codec_name", ""),
        "vprofile": str((v or {}).get("profile", "")),
        "pix_fmt": (v or {}).get("pix_fmt", ""),
        "width": int((v or {}).get("width") or 0),
        "height": int((v or {}).get("height") or 0),
        "vbitrate_kbps": int((v or {}).get("bit_rate") or 0) // 1000,
        "n_video": sum(1 for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) != 1),
        "n_attach": sum(1 for s in streams if s.get("codec_type") == "attachment" or s.get("disposition", {}).get("attached_pic", 0) == 1),
        "n_audio": sum(1 for s in streams if s.get("codec_type") == "audio"),
        "n_sub": sum(1 for s in streams if s.get("codec_type") == "subtitle"),
        "n_streams": len(streams),
    }
    # --- débit audio total (bit_rate du flux, sinon tag BPS)
    def _kbps(st):
        v = st.get("bit_rate")
        if not v or not str(v).isdigit():
            v = st.get("tags", {}).get("BPS") or st.get("tags", {}).get("BPS-eng") or 0
        return int(v) // 1000 if str(v).isdigit() else 0
    out["audio"] = [{"codec": s.get("codec_name", ""), "channels": int(s.get("channels") or 2),
                     "kbps": _kbps(s), "lang": s.get("tags", {}).get("language", "")}
                    for s in streams if s.get("codec_type") == "audio"]
    out["abitrate_kbps"] = sum(a["kbps"] for a in out["audio"])
    # --- débit vidéo : flux > (total - audio) > tag BPS seulement s'il est cohérent avec la taille du fichier
    derived = bitrate - out["abitrate_kbps"] if bitrate else 0
    tag = _kbps(v) if v else 0
    if out["vbitrate_kbps"] and out["vbitrate_kbps"] <= bitrate * 1.02:
        pass  # bit_rate du flux vidéo fiable
    elif derived > 0:
        out["vbitrate_kbps"] = derived if not tag or tag > bitrate else min(tag, derived)
    elif tag and (not bitrate or tag <= bitrate):
        out["vbitrate_kbps"] = tag
    else:
        out["vbitrate_kbps"] = int(bitrate * 0.85)
    return out


def probe_into(mf, local_path, cfg):
    """Analyse local_path et remplit le MediaFile mf (sans sauvegarder)."""
    try:
        st = os.stat(local_path)
        mf.size, mf.mtime = st.st_size, st.st_mtime
        s = summarize(ffprobe(local_path))
        mf.container = s["container"]
        mf.duration = s["duration"]
        mf.bitrate_kbps = s["bitrate_kbps"]
        mf.vcodec = s["vcodec"]
        mf.vprofile = s["vprofile"][:50]
        mf.pix_fmt = s["pix_fmt"]
        mf.width, mf.height = s["width"], s["height"]
        mf.vbitrate_kbps = s["vbitrate_kbps"]
        mf.abitrate_kbps = s["abitrate_kbps"]
        mf.audio_info = s["audio"]
        mf.audio_streams, mf.sub_streams = s["n_audio"], s["n_sub"]
        mf.probe_error = ""
    except Exception as e:  # noqa: BLE001
        mf.probe_error = f"{type(e).__name__}: {e}"[:2000]
    mf.probed_at = timezone.now()
    mf.apply_detection(cfg)
    return mf
