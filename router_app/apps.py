import logging
import sys

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)

# Commands that must not (or need not) load the station index.
_SKIP_WARMUP_COMMANDS = {
    'geocode_stations', 'makemigrations', 'migrate', 'collectstatic', 'test', 'check',
}


class RouterAppConfig(AppConfig):
    """Loads stations + KD-tree and the gazetteer once, at server startup."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'router_app'

    def ready(self) -> None:
        """Warm the in-memory indexes so the first request is not slower.

        Skipped for management commands that rebuild or don't need the data,
        and when the geocoded file doesn't exist yet (requests then return a
        clear 503 telling the operator to run ``geocode_stations``).
        """
        if len(sys.argv) > 1 and sys.argv[1] in _SKIP_WARMUP_COMMANDS:
            return
        if not settings.STATIONS_GEOCODED_JSON.exists():
            logger.warning('%s not found - run `python manage.py geocode_stations`.',
                           settings.STATIONS_GEOCODED_JSON)
            return
        from .services.geocoding import get_gazetteer
        from .services.spatial import get_station_index
        try:
            get_station_index()
            get_gazetteer()
        except Exception:  # never block startup; requests will report errors
            logger.exception('Failed to warm up station index.')
