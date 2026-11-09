"""Accès aux serveurs sources : navigation SFTP (côté web) et montage SSHFS (côté worker)."""
import os
import stat
import subprocess
import threading
import time

import paramiko

from .models import MEDIA_EXTS

_mount_lock = threading.Lock()


def _client(server, timeout=15):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=server.host, port=server.port, username=server.username, timeout=timeout,
              banner_timeout=timeout, auth_timeout=timeout, allow_agent=False, look_for_keys=False)
    if server.key_file:
        kw["key_filename"] = server.key_path()
    if server.password:
        kw["password"] = server.password
    c.connect(**kw)
    return c


def test_connection(server):
    """Retourne (ok, message)."""
    try:
        c = _client(server)
        sftp = c.open_sftp()
        sftp.listdir(server.base_path)
        sftp.close()
        c.close()
        return True, "Connexion SSH/SFTP OK"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def sftp_listdir(server, path):
    """Liste un répertoire distant : (dirs, files) triés, files = dicts {name, path, size, mtime, is_media}."""
    c = _client(server)
    try:
        sftp = c.open_sftp()
        dirs, files = [], []
        for a in sftp.listdir_attr(path):
            full = os.path.join(path, a.filename)
            if stat.S_ISDIR(a.st_mode):
                dirs.append({"name": a.filename, "path": full})
            elif stat.S_ISREG(a.st_mode):
                ext = os.path.splitext(a.filename)[1].lower()
                files.append({"name": a.filename, "path": full, "size": a.st_size or 0,
                              "mtime": a.st_mtime or 0, "is_media": ext in MEDIA_EXTS})
        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())
        return dirs, files
    finally:
        c.close()


# ---------------------------------------------------------------- SSHFS (worker)

def is_mounted(path):
    try:
        return os.path.ismount(path)
    except OSError:
        return False


def ensure_mounted(server, log=None):
    """Monte base_path du serveur sur server.mount_point via sshfs (idempotent)."""
    mp = server.mount_point
    with _mount_lock:
        if is_mounted(mp):
            # vérifie que le montage répond (sinon "Transport endpoint is not connected")
            try:
                os.listdir(mp)
                return mp
            except OSError:
                unmount(server)
        os.makedirs(mp, exist_ok=True)
        opts = [
            "reconnect", "ServerAliveInterval=15", "ServerAliveCountMax=3",
            "StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null", "LogLevel=ERROR",
            "follow_symlinks", "kernel_cache", "auto_cache",
            f"port={server.port}",
        ]
        if server.key_file:
            opts.append(f"IdentityFile={server.key_path()}")
        cmd = ["sshfs", f"{server.username}@{server.host}:{server.base_path}", mp, "-o", ",".join(opts)]
        stdin = None
        if server.password and not server.key_file:
            opts.append("password_stdin")
            cmd = ["sshfs", f"{server.username}@{server.host}:{server.base_path}", mp, "-o", ",".join(opts)]
            stdin = server.password + "\n"
        if log:
            log(f"[mount] {' '.join(cmd)}\n")
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"sshfs a échoué ({r.returncode}) : {r.stderr.strip()}")
        for _ in range(20):
            if is_mounted(mp):
                tune_readahead(mp, log)
                return mp
            time.sleep(0.25)
        raise RuntimeError("sshfs : point de montage non actif après 5 s")


def tune_readahead(mp, log=None, kb=16384):
    """Augmente la lecture anticipée du montage FUSE (128 Ko par défaut → 16 Mo) : x6 sur le débit sshfs."""
    try:
        dev = os.stat(mp).st_dev
        path = f"/sys/devices/virtual/bdi/{os.major(dev)}:{os.minor(dev)}/read_ahead_kb"
        with open(path, "w") as f:
            f.write(str(kb))
        if log:
            log(f"[mount] readahead {mp} -> {kb} Ko\n")
    except OSError as e:
        if log:
            log(f"[mount] readahead non réglé ({e}) : débit sshfs réduit\n")


def unmount(server):
    mp = server.mount_point
    subprocess.run(["fusermount3", "-uz", mp], capture_output=True)
    subprocess.run(["fusermount", "-uz", mp], capture_output=True)


def unmount_all():
    from .models import SourceServer
    for s in SourceServer.objects.all():
        if is_mounted(s.mount_point):
            unmount(s)


def remote_find(server, term, path=None, limit=300):
    """Recherche de fichiers par nom (insensible à la casse) via `find` sur le serveur. Retourne [{path,name,size}]."""
    import shlex
    root = path or server.base_path
    exts = " -o ".join(f"-iname '*{e}'" for e in sorted(MEDIA_EXTS))
    pat = shlex.quote(f"*{term}*")
    cmd = (f"find {shlex.quote(root)} -type f -iname {pat} \\( {exts} \\) -printf '%s\\t%p\\n' 2>/dev/null | head -n {int(limit)}")
    c = _client(server, timeout=20)
    try:
        _, out, _ = c.exec_command(cmd, timeout=120)
        res = []
        for line in out.read().decode("utf-8", "replace").splitlines():
            if "\t" in line:
                size, p = line.split("\t", 1)
                res.append({"path": p, "name": os.path.basename(p), "size": int(size) if size.isdigit() else 0})
        return res
    finally:
        c.close()
