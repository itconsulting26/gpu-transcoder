import os
from django.db import models
from django.utils import timezone

MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".wmv", ".webm", ".flv", ".mpg", ".mpeg"}
SUPPORTED_CODECS = {"h264", "hevc"}


class Config(models.Model):
    """Réglages modifiables depuis l'interface (singleton)."""
    size_ratio = models.FloatField("Cible : fraction du débit vidéo d'origine (0.5 = −50 %)", default=0.5)
    max_bitrate_kbps = models.PositiveIntegerField("Débit vidéo max (kb/s) — plafond absolu", default=4000)
    min_bitrate_kbps = models.PositiveIntegerField("Débit vidéo mini (kb/s) — plancher de qualité", default=1200)
    target_bitrate_kbps = models.PositiveIntegerField("Débit vidéo cible fixe (kb/s) si la fraction est à 0", default=3500)
    cq = models.PositiveSmallIntegerField("Qualité NVENC cq (0 = désactivé, pilotage par débit = taille prévisible)", default=0)
    preset = models.CharField("Preset NVENC (p1 rapide … p7 lent)", max_length=4, default="p4")
    fast_mode = models.BooleanField("Mode rapide (sans tune hq ni AQ ; un peu moins de qualité)", default=True)
    concurrency = models.PositiveSmallIntegerField("Transcodages simultanés", default=2)
    detect_min_size_gb = models.FloatField("Détection : taille mini (Go)", default=1.5)
    detect_min_bitrate_kbps = models.PositiveIntegerField("Détection : débit total mini (kb/s)", default=4000)
    verify_duration_tolerance_s = models.FloatField("Tolérance de durée à la vérification (s)", default=2.0)
    audio_reencode = models.BooleanField("Réencoder l'audio trop lourd (toutes les pistes sont gardées)", default=True)
    audio_stereo_kbps = models.PositiveIntegerField("Audio ≤ 2 canaux : débit cible (kb/s, AAC)", default=128)
    audio_surround_kbps = models.PositiveIntegerField("Audio > 2 canaux : débit cible (kb/s, E-AC3)", default=256)
    min_gain_pct = models.PositiveSmallIntegerField("Gain minimal estimé pour lancer l'encodage (%)", default=15)
    keep_backup = models.BooleanField("Garder une copie .bak de l'original (déconseillé : double l'espace)", default=False)
    paused = models.BooleanField("Mettre la file en pause", default=False)

    class Meta:
        verbose_name = "Réglages"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Réglages"


class SourceServer(models.Model):
    name = models.CharField("Nom", max_length=100)
    host = models.CharField("Hôte / IP", max_length=200)
    port = models.PositiveIntegerField("Port SSH", default=22)
    username = models.CharField("Utilisateur", max_length=100, default="root")
    password = models.CharField("Mot de passe (si pas de clé)", max_length=200, blank=True)
    key_file = models.CharField("Clé privée (nom de fichier dans keys/)", max_length=200, blank=True)
    base_path = models.CharField("Répertoire racine", max_length=500, default="/")
    enabled = models.BooleanField("Actif", default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Serveur source"
        verbose_name_plural = "Serveurs sources"

    def __str__(self):
        return f"{self.name} ({self.username}@{self.host}:{self.port})"

    @property
    def mount_point(self):
        from django.conf import settings
        return os.path.join(settings.MOUNT_ROOT, str(self.pk))

    def key_path(self):
        from django.conf import settings
        return os.path.join(settings.KEYS_DIR, self.key_file) if self.key_file else None

    def local_path(self, remote_path):
        """Chemin local (via sshfs) d'un chemin distant absolu."""
        rel = os.path.relpath(remote_path, self.base_path)
        return os.path.normpath(os.path.join(self.mount_point, rel))


class MediaFile(models.Model):
    STATUS = [
        ("new", "Détecté"),
        ("queued", "En file"),
        ("running", "En cours"),
        ("done", "Transcodé"),
        ("nogain", "Sans gain"),
        ("error", "Erreur"),
        ("ignored", "Ignoré"),
    ]
    server = models.ForeignKey(SourceServer, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=1000)  # chemin distant absolu
    name = models.CharField(max_length=300)
    size = models.BigIntegerField(default=0)
    mtime = models.FloatField(default=0)
    container = models.CharField(max_length=50, blank=True)
    vcodec = models.CharField(max_length=30, blank=True)
    vprofile = models.CharField(max_length=50, blank=True)
    pix_fmt = models.CharField(max_length=30, blank=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    duration = models.FloatField(default=0)
    bitrate_kbps = models.PositiveIntegerField(default=0)  # débit total
    vbitrate_kbps = models.PositiveIntegerField(default=0)  # débit vidéo (calculé : total - audio si besoin)
    abitrate_kbps = models.PositiveIntegerField(default=0)  # débit audio cumulé
    audio_info = models.JSONField(default=list, blank=True)  # [{codec, channels, kbps, lang}]
    audio_streams = models.PositiveSmallIntegerField(default=0)
    sub_streams = models.PositiveSmallIntegerField(default=0)
    probed_at = models.DateTimeField(null=True, blank=True)
    probe_error = models.TextField(blank=True)
    needs_transcode = models.BooleanField(default=False)
    supported = models.BooleanField(default=False)  # H.264 / HEVC
    status = models.CharField(max_length=10, choices=STATUS, default="new")
    size_after = models.BigIntegerField(null=True, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("server", "path")
        ordering = ["-needs_transcode", "-size"]

    def __str__(self):
        return self.path

    @property
    def size_gb(self):
        return self.size / 1e9

    @property
    def ext(self):
        return os.path.splitext(self.name)[1].lower()

    @property
    def is_10bit(self):
        return "10" in (self.pix_fmt or "")

    def apply_detection(self, cfg):
        self.supported = self.vcodec in SUPPORTED_CODECS
        self.needs_transcode = bool(
            self.duration > 0
            and (self.size > cfg.detect_min_size_gb * 1e9 or self.bitrate_kbps > cfg.detect_min_bitrate_kbps)
        )


class Job(models.Model):
    STATUS = [
        ("queued", "En attente"),
        ("running", "En cours"),
        ("cancelling", "Annulation…"),
        ("done", "Terminé"),
        ("nogain", "Sans gain (original conservé)"),
        ("error", "Erreur"),
        ("cancelled", "Annulé"),
    ]
    PHASES = [
        ("wait", "En attente"),
        ("mount", "Montage SSHFS"),
        ("probe", "Analyse"),
        ("encode", "Transcodage"),
        ("remux", "Copie + sous-titres"),
        ("verify", "Vérification"),
        ("replace", "Remplacement"),
        ("end", "Fin"),
    ]
    file = models.ForeignKey(MediaFile, on_delete=models.CASCADE, related_name="jobs")
    status = models.CharField(max_length=12, choices=STATUS, default="queued")
    phase = models.CharField(max_length=10, choices=PHASES, default="wait")
    priority = models.IntegerField(default=0)  # plus haut = plus tôt
    force = models.BooleanField(default=False)  # transcoder même si hors H.264/HEVC ou sans détection
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)
    pid = models.IntegerField(null=True, blank=True)
    attempt = models.PositiveSmallIntegerField(default=0)
    # progression
    percent = models.FloatField(default=0)
    fps = models.FloatField(default=0)
    speed = models.FloatField(default=0)  # x temps réel
    out_time_s = models.FloatField(default=0)
    out_bitrate_kbps = models.FloatField(default=0)
    out_size = models.BigIntegerField(default=0)
    eta_s = models.FloatField(null=True, blank=True)
    frame = models.BigIntegerField(default=0)
    # résultat
    target_codec = models.CharField(max_length=20, blank=True)
    ffmpeg_cmd = models.TextField(blank=True)
    log = models.TextField(blank=True)
    error = models.TextField(blank=True)
    size_before = models.BigIntegerField(default=0)
    size_after = models.BigIntegerField(null=True, blank=True)
    hw_decode = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"Job #{self.pk} {self.file.name}"

    @property
    def is_active(self):
        return self.status in ("queued", "running", "cancelling")

    @property
    def elapsed_s(self):
        if not self.started:
            return 0
        end = self.finished or timezone.now()
        return (end - self.started).total_seconds()

    @property
    def gain_pct(self):
        if self.size_before and self.size_after:
            return 100 * (1 - self.size_after / self.size_before)
        return None

    def append_log(self, text):
        self.log = (self.log + text)[-20000:]


class ScanTask(models.Model):
    STATUS = [("queued", "En attente"), ("running", "En cours"), ("done", "Terminé"), ("error", "Erreur"), ("cancelled", "Annulé")]
    server = models.ForeignKey(SourceServer, on_delete=models.CASCADE, related_name="scans")
    path = models.CharField(max_length=1000)
    recursive = models.BooleanField(default=True)
    status = models.CharField(max_length=10, choices=STATUS, default="queued")
    files_seen = models.PositiveIntegerField(default=0)
    files_probed = models.PositiveIntegerField(default=0)
    files_flagged = models.PositiveIntegerField(default=0)
    current = models.CharField(max_length=1000, blank=True)
    error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"Scan {self.server.name}:{self.path}"


class WorkerStatus(models.Model):
    """État du worker et du GPU, rafraîchi par le worker (singleton)."""
    updated = models.DateTimeField(auto_now=True)
    alive = models.BooleanField(default=False)
    gpu_name = models.CharField(max_length=100, blank=True)
    gpu_util = models.FloatField(default=0)
    gpu_enc = models.FloatField(default=0)
    gpu_dec = models.FloatField(default=0)
    gpu_mem_used = models.PositiveIntegerField(default=0)
    gpu_mem_total = models.PositiveIntegerField(default=0)
    gpu_temp = models.FloatField(default=0)
    gpu_power = models.FloatField(default=0)
    message = models.TextField(blank=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
