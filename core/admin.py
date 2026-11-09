from django.contrib import admin
from .models import Config, Job, MediaFile, ScanTask, SourceServer, WorkerStatus

admin.site.register(Config)
admin.site.register(SourceServer)
admin.site.register(ScanTask)
admin.site.register(WorkerStatus)


@admin.register(MediaFile)
class MediaFileAdmin(admin.ModelAdmin):
    list_display = ("name", "server", "size", "vcodec", "bitrate_kbps", "needs_transcode", "status")
    list_filter = ("server", "status", "needs_transcode", "vcodec")
    search_fields = ("path",)


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "file", "status", "phase", "percent", "created")
    list_filter = ("status",)
