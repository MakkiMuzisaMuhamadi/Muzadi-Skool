from django.contrib import admin
from django.templatetags.static import static
from django.urls import path, include, re_path
from django.views.generic.base import RedirectView

from mainApp.views.media_views import protected_media
from mainApp.views import pwa_views

urlpatterns = [
    path('admin/', admin.site.urls),
    # Installable app (PWA). sw.js must live at the root so it covers every page.
    path('manifest.webmanifest', pwa_views.manifest, name='pwa_manifest'),
    path('sw.js', pwa_views.service_worker, name='pwa_service_worker'),
    path('offline/', pwa_views.offline, name='pwa_offline'),
    # Browsers ask for /favicon.ico on their own (PDFs, admin, error pages).
    path('favicon.ico', RedirectView.as_view(url=static('images/favicon.ico'), permanent=True), name='favicon'),
    # Uploaded files are private: the view checks the school before sending the file
    # (on the server nginx then sends it - see deploy/nginx/school360.conf).
    re_path(r'^media/(?P<path>.+)$', protected_media, name='protected_media'),
    path('', include('mainApp.urls')),
]
