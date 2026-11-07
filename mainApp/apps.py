from django.apps import AppConfig


class MainappConfig(AppConfig):
    name = 'mainApp'

    def ready(self):
        # Keeps teachers' allowed levels in sync with their allocations
        from mainApp import teacher_signals  # noqa: F401
        # Login / logout / failed-login audit trail
        from mainApp import signals  # noqa: F401

        # Load every model module so the cross-school guard can see all of them
        # (some are otherwise only imported when the URLs load).
        import importlib
        import pkgutil

        import mainApp.models as models_pkg
        for info in pkgutil.iter_modules(models_pkg.__path__):
            importlib.import_module(f"mainApp.models.{info.name}")

        from mainApp import tenancy
        tenancy.connect_guard()

        # Platform signals (new school -> subscription + messaging account)
        from mainApp import platform_signals  # noqa: F401
