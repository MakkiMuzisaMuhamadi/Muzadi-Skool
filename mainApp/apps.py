from django.apps import AppConfig


class MainappConfig(AppConfig):
    name = 'mainApp'

    def ready(self):
        # Keeps teachers' allowed levels in sync with their allocations
        from mainApp import teacher_signals  # noqa: F401
