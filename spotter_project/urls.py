"""
URL configuration for spotter_project.
"""
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    path('api/', include('router_app.urls')),
    # Redirect root to the map UI
    path('', RedirectView.as_view(url='/api/map/', permanent=False)),
]
