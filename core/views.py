import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ConfigForm, ServerForm
from .models import Config, Job, MediaFile, ScanTask, SourceServer, WorkerStatus
from .ssh import sftp_listdir, test_connection


def _job_dict(j):
    return {
        "id": j.pk, "name": j.file.name, "path": j.file.path, "server": j.file.server.name,
        "status": j.status, "status_label": j.get_status_display(), "phase": j.phase,
        "phase_label": j.get_phase_display(), "percent": j.percent, "fps": j.fps, "speed": j.speed,
        "out_time_s": j.out_time_s, "duration": j.file.duration, "out_bitrate_kbps": j.out_bitrate_kbps,
        "out_size": j.out_size, "size_before": j.size_before or j.file.size, "eta_s": j.eta_s,
        "elapsed_s": j.elapsed_s, "codec": j.target_codec, "hw": j.hw_decode, "error": j.error[:300],
        "size_after": j.size_after, "gain_pct": j.gain_pct,
    }


@login_required
def api_status(request):
    ws = WorkerStatus.get()
    alive = ws.alive and (timezone.now() - ws.updated).total_seconds() < 20
    active = Job.objects.filter(status__in=("running", "cancelling")).select_related("file", "file__server")
    queued = Job.objects.filter(status="queued").select_related("file", "file__server").order_by("-priority", "created")[:30]
    recent = Job.objects.filter(status__in=("done", "nogain", "error", "cancelled")).select_related("file", "file__server")[:10]
    scans = ScanTask.objects.filter(status__in=("queued", "running")).select_related("server")
    cfg = Config.get()
    return JsonResponse({
        "worker": {"alive": alive, "updated": ws.updated.isoformat(), "paused": cfg.paused,
                   "concurrency": cfg.concurrency, "message": ws.message},
        "gpu": {"name": ws.gpu_name, "util": ws.gpu_util, "enc": ws.gpu_enc, "dec": ws.gpu_dec,
                "mem_used": ws.gpu_mem_used, "mem_total": ws.gpu_mem_total, "temp": ws.gpu_temp,
                "power": ws.gpu_power},
        "active": [_job_dict(j) for j in active],
        "queued": [_job_dict(j) for j in queued],
        "queued_count": Job.objects.filter(status="queued").count(),
        "recent": [_job_dict(j) for j in recent],
        "scans": [{"id": s.pk, "server": s.server.name, "path": s.path, "status": s.status,
                   "seen": s.files_seen, "probed": s.files_probed, "flagged": s.files_flagged,
                   "current": os.path.basename(s.current)} for s in scans],
        "now": timezone.now().isoformat(),
    })


@login_required
def dashboard(request):
    stats = {
        "files": MediaFile.objects.count(),
        "flagged": MediaFile.objects.filter(needs_transcode=True, status="new").count(),
        "done": Job.objects.filter(status="done").count(),
        "saved": Job.objects.filter(status="done").aggregate(s=Sum("size_before"), a=Sum("size_after")),
        "servers": SourceServer.objects.count(),
    }
    s = stats["saved"]
    stats["saved_bytes"] = (s["s"] or 0) - (s["a"] or 0)
    return render(request, "dashboard.html", {"stats": stats, "cfg": Config.get()})


# ------------------------------------------------------------------ serveurs

@login_required
def servers(request):
    qs = SourceServer.objects.annotate(n_files=Count("files"), n_flagged=Count("files", filter=Q(files__needs_transcode=True, files__status="new")))
    keys = sorted(os.listdir("/keys")) if os.path.isdir("/keys") else []
    return render(request, "servers.html", {"servers": qs, "keys": keys})


@login_required
def server_edit(request, pk=None):
    srv = get_object_or_404(SourceServer, pk=pk) if pk else None
    form = ServerForm(request.POST or None, instance=srv)
    if request.method == "POST" and form.is_valid():
        srv = form.save()
        ok, msg = test_connection(srv)
        (messages.success if ok else messages.warning)(request, f"{srv.name} enregistré. {msg}")
        return redirect("servers")
    keys = sorted(os.listdir("/keys")) if os.path.isdir("/keys") else []
    return render(request, "server_form.html", {"form": form, "server": srv, "keys": keys})


@login_required
@require_POST
def server_delete(request, pk):
    srv = get_object_or_404(SourceServer, pk=pk)
    if Job.objects.filter(file__server=srv, status__in=("running", "cancelling")).exists():
        messages.error(request, "Un transcodage est en cours sur ce serveur.")
    else:
        srv.delete()
        messages.success(request, "Serveur supprimé (fichiers et historique associés inclus).")
    return redirect("servers")


@login_required
@require_POST
def server_test(request, pk):
    srv = get_object_or_404(SourceServer, pk=pk)
    ok, msg = test_connection(srv)
    (messages.success if ok else messages.error)(request, f"{srv.name} : {msg}")
    return redirect("servers")


@login_required
def browse(request, pk):
    srv = get_object_or_404(SourceServer, pk=pk)
    path = request.GET.get("path") or srv.base_path
    path = os.path.normpath(path)
    if not path.startswith(os.path.normpath(srv.base_path)):
        path = srv.base_path
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "scan":
            ScanTask.objects.create(server=srv, path=path, recursive=request.POST.get("recursive") == "1")
            messages.success(request, f"Scan lancé sur {path}")
            return redirect("scans")
        if action in ("queue", "queue_force"):
            n = _queue_paths(srv, request.POST.getlist("sel"), force=(action == "queue_force"))
            messages.success(request, f"{n} fichier(s) ajouté(s) à la file")
        return redirect(f"{request.path}?path={path}")
    error = None
    dirs = files = []
    try:
        dirs, files = sftp_listdir(srv, path)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    known = {m.path: m for m in MediaFile.objects.filter(server=srv, path__in=[f["path"] for f in files])}
    for f in files:
        f["mf"] = known.get(f["path"])
    parent = os.path.dirname(path) if path != srv.base_path and len(path) > len(srv.base_path) else None
    crumbs = []
    rel = os.path.relpath(path, srv.base_path)
    acc = srv.base_path
    if rel != ".":
        for part in rel.split(os.sep):
            acc = os.path.join(acc, part)
            crumbs.append({"name": part, "path": acc})
    return render(request, "browse.html", {"server": srv, "path": path, "dirs": dirs, "files": files,
                                           "parent": parent, "crumbs": crumbs, "error": error})


def _queue_paths(srv, paths, force=False):
    n = 0
    for p in paths:
        mf, _ = MediaFile.objects.get_or_create(server=srv, path=p, defaults={"name": os.path.basename(p)})
        if mf.status in ("queued", "running"):
            continue
        Job.objects.create(file=mf, force=force)
        mf.status = "queued"
        mf.save(update_fields=["status"])
        n += 1
    return n


# ------------------------------------------------------------------ fichiers

@login_required
def files(request):
    qs = MediaFile.objects.select_related("server")
    f = request.GET.get("f", "flagged")
    if f == "flagged":
        qs = qs.filter(needs_transcode=True, status="new")
    elif f == "unsupported":
        qs = qs.filter(supported=False, probed_at__isnull=False, probe_error="")
    elif f == "errors":
        qs = qs.filter(Q(status="error") | ~Q(probe_error=""))
    elif f == "done":
        qs = qs.filter(status__in=("done", "nogain"))
    srv = request.GET.get("server")
    if srv:
        qs = qs.filter(server_id=srv)
    q = request.GET.get("q")
    if q:
        qs = qs.filter(path__icontains=q)
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render(request, "files.html", {"page": page, "f": f, "q": q or "", "srv": srv or "",
                                          "servers": SourceServer.objects.all(), "total": qs.count()})


@login_required
@require_POST
def files_action(request):
    ids = request.POST.getlist("sel")
    action = request.POST.get("action")
    qs = MediaFile.objects.filter(pk__in=ids)
    n = 0
    if action in ("queue", "queue_force"):
        for mf in qs.exclude(status__in=("queued", "running")):
            Job.objects.create(file=mf, force=(action == "queue_force"))
            mf.status = "queued"
            mf.save(update_fields=["status"])
            n += 1
        messages.success(request, f"{n} fichier(s) ajouté(s) à la file")
    elif action == "ignore":
        n = qs.exclude(status__in=("queued", "running")).update(status="ignored")
        messages.success(request, f"{n} fichier(s) ignoré(s)")
    elif action == "unignore":
        n = qs.filter(status="ignored").update(status="new")
        messages.success(request, f"{n} fichier(s) réactivé(s)")
    elif action == "forget":
        n, _ = qs.exclude(status__in=("queued", "running")).delete()
        messages.success(request, f"{n} entrée(s) supprimée(s) de la base (les fichiers eux-mêmes ne sont pas touchés)")
    return redirect(request.POST.get("next") or "files")


# ------------------------------------------------------------------ jobs

@login_required
def jobs(request):
    qs = Job.objects.select_related("file", "file__server")
    st = request.GET.get("status")
    if st:
        qs = qs.filter(status=st)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "jobs.html", {"page": page, "st": st or "", "statuses": Job.STATUS})


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job.objects.select_related("file", "file__server"), pk=pk)
    return render(request, "job_detail.html", {"job": job})


@login_required
@require_POST
def job_action(request, pk):
    job = get_object_or_404(Job, pk=pk)
    action = request.POST.get("action")
    if action == "cancel" and job.status in ("queued", "running"):
        job.status = "cancelling" if job.status == "running" else "cancelled"
        if job.status == "cancelled":
            job.finished = timezone.now()
            job.file.status = "new"
            job.file.save(update_fields=["status"])
        job.save()
        messages.info(request, "Annulation demandée")
    elif action == "retry" and job.status in ("error", "cancelled", "nogain"):
        new = Job.objects.create(file=job.file, force=job.force, priority=job.priority)
        job.file.status = "queued"
        job.file.save(update_fields=["status"])
        messages.success(request, f"Nouveau job #{new.pk} créé")
        return redirect("job_detail", pk=new.pk)
    elif action == "top" and job.status == "queued":
        job.priority = (Job.objects.filter(status="queued").order_by("-priority").values_list("priority", flat=True).first() or 0) + 1
        job.save(update_fields=["priority"])
    elif action == "delete" and not job.is_active:
        job.delete()
        return redirect("jobs")
    nxt = request.POST.get("next")
    return redirect(nxt) if nxt else redirect("job_detail", pk=pk)


# ------------------------------------------------------------------ scans / réglages

@login_required
def scans(request):
    return render(request, "scans.html", {"scans": ScanTask.objects.select_related("server")[:100]})


@login_required
@require_POST
def scan_cancel(request, pk):
    ScanTask.objects.filter(pk=pk, status__in=("queued", "running")).update(status="cancelled", finished=timezone.now())
    return redirect("scans")


@login_required
def settings_view(request):
    cfg = Config.get()
    form = ConfigForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Réglages enregistrés")
        return redirect("settings")
    return render(request, "settings.html", {"form": form})


# ------------------------------------------------------------------ recherche

@login_required
def search(request):
    from .ssh import remote_find
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    srv_id = request.GET.get("server") or request.POST.get("server") or ""
    servers_all = SourceServer.objects.filter(enabled=True)
    if request.method == "POST":
        sel = request.POST.getlist("sel")  # "server_id|path"
        n = 0
        force = request.POST.get("action") == "queue_force"
        by_srv = {}
        for s in sel:
            sid, p = s.split("|", 1)
            by_srv.setdefault(int(sid), []).append(p)
        for sid, paths in by_srv.items():
            n += _queue_paths(SourceServer.objects.get(pk=sid), paths, force=force)
        messages.success(request, f"{n} fichier(s) ajouté(s) à la file")
        return redirect(f"{request.path}?q={q}&server={srv_id}")
    results, errors = [], []
    if q:
        servers = servers_all.filter(pk=srv_id) if srv_id else servers_all
        known = {(m.server_id, m.path): m for m in MediaFile.objects.filter(name__icontains=q).select_related("server")}
        seen = set()
        for s in servers:
            try:
                for r in remote_find(s, q):
                    key = (s.pk, r["path"])
                    seen.add(key)
                    results.append({"server": s, "path": r["path"], "name": r["name"], "size": r["size"], "mf": known.get(key)})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{s.name} : {type(e).__name__}: {e}")
        # fichiers connus en base mais non retournés par find (serveur injoignable, etc.)
        for key, m in known.items():
            if key not in seen and (not srv_id or str(m.server_id) == srv_id):
                results.append({"server": m.server, "path": m.path, "name": m.name, "size": m.size, "mf": m})
        results.sort(key=lambda r: (r["server"].name, r["name"].lower()))
    return render(request, "search.html", {"q": q, "srv": srv_id, "servers": servers_all, "results": results, "errors": errors})


# ------------------------------------------------------------------ utilisateurs

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.decorators import user_passes_test  # noqa: E402

admin_required = user_passes_test(lambda u: u.is_superuser, login_url="/")


@login_required
@admin_required
def users(request):
    User = get_user_model()
    return render(request, "users.html", {"users": User.objects.order_by("username")})


@login_required
@admin_required
@require_POST
def user_add(request):
    User = get_user_model()
    name, pw = request.POST.get("username", "").strip(), request.POST.get("password", "")
    if not name or len(pw) < 8:
        messages.error(request, "Nom requis et mot de passe d'au moins 8 caractères.")
    elif User.objects.filter(username=name).exists():
        messages.error(request, "Ce nom existe déjà.")
    else:
        is_admin = request.POST.get("admin") == "1"
        User.objects.create_user(username=name, password=pw, is_staff=is_admin, is_superuser=is_admin)
        messages.success(request, f"Utilisateur {name} créé" + (" (admin)" if is_admin else ""))
    return redirect("users")


@login_required
@admin_required
@require_POST
def user_password(request, pk):
    User = get_user_model()
    u = get_object_or_404(User, pk=pk)
    pw = request.POST.get("password", "")
    if len(pw) < 8:
        messages.error(request, "Mot de passe d'au moins 8 caractères.")
    else:
        u.set_password(pw)
        u.save()
        messages.success(request, f"Mot de passe de {u.username} modifié")
        if u == request.user:
            from django.contrib.auth import update_session_auth_hash
            update_session_auth_hash(request, u)
    return redirect("users")


@login_required
@admin_required
@require_POST
def user_toggle(request, pk):
    User = get_user_model()
    u = get_object_or_404(User, pk=pk)
    if u == request.user:
        messages.error(request, "Impossible de modifier son propre rôle.")
    else:
        u.is_superuser = u.is_staff = not u.is_superuser
        u.save()
        messages.success(request, f"{u.username} est maintenant {'admin' if u.is_superuser else 'utilisateur'}")
    return redirect("users")


@login_required
@admin_required
@require_POST
def user_delete(request, pk):
    User = get_user_model()
    u = get_object_or_404(User, pk=pk)
    if u == request.user:
        messages.error(request, "Impossible de supprimer son propre compte.")
    else:
        u.delete()
        messages.success(request, "Utilisateur supprimé")
    return redirect("users")
