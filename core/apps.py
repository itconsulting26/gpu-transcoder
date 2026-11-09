from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "Transcodage"

    def ready(self):
        from django.db.backends.signals import connection_created

        def _sqlite_pragmas(sender, connection, **kwargs):
            if connection.vendor == "sqlite":
                cur = connection.cursor()
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")
                cur.execute("PRAGMA busy_timeout=30000;")

        connection_created.connect(_sqlite_pragmas)
