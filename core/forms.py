from django import forms
from .models import Config, SourceServer


class ServerForm(forms.ModelForm):
    class Meta:
        model = SourceServer
        fields = ["name", "host", "port", "username", "password", "key_file", "base_path", "enabled"]
        widgets = {"password": forms.PasswordInput(render_value=True)}


class ConfigForm(forms.ModelForm):
    class Meta:
        model = Config
        fields = ["size_ratio", "max_bitrate_kbps", "min_bitrate_kbps", "target_bitrate_kbps", "cq", "preset", "fast_mode", "audio_reencode", "audio_stereo_kbps", "audio_surround_kbps", "concurrency",
                  "detect_min_size_gb", "detect_min_bitrate_kbps", "min_gain_pct", "verify_duration_tolerance_s",
                  "keep_backup", "paused"]
