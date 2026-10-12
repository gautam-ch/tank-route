"""
router_app URL configuration.
"""
from django.urls import path
from . import views

app_name = 'router_app'

urlpatterns = [
    # Main fuel route API endpoint
    # GET /api/route/?start=New+York,NY&finish=Los+Angeles,CA&strategy=full_tank
    path('route/', views.FuelRouteView.as_view(), name='fuel_route'),

    # Interactive map UI
    path('map/', views.MapView.as_view(), name='map'),

    # Health check
    path('health/', views.HealthCheckView.as_view(), name='health'),
]
