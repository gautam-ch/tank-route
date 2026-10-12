"""
Django settings for spotter_project project.
Django 6.1 — Spotter Fuel Route Planner API
"""

from pathlib import Path
import os

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
# NOTE: Replace this with a proper secret in production (use env variable).
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-dev-secret-spotter-fuel-route-planner-change-in-prod'
)

DEBUG = os.environ.get('DJANGO_DEBUG', 'True') == 'True'

ALLOWED_HOSTS = ['*']  # Restrict in production

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'corsheaders',
    # Our app
    'router_app',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',  # Must be first
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'spotter_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'spotter_project.wsgi.application'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# ---------------------------------------------------------------------------
# Caching — Local memory cache (dev). Replace with Redis in production.
# ---------------------------------------------------------------------------
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'spotter-fuel-cache',
        'TIMEOUT': 86400,  # 24 hours — geocode results are stable
        'OPTIONS': {
            'MAX_ENTRIES': 5000,
        },
    },
    # Disk-backed cache for external API results (OSRM routes, Nominatim
    # geocodes). Survives server restarts, so a route is fetched from OSRM
    # only once per week. Entries are pickled + zlib-compressed by Django;
    # a coast-to-coast route is a few hundred KB, so entries are capped.
    'persistent': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': BASE_DIR / 'data' / 'cache',
        'TIMEOUT': 7 * 86400,
        'OPTIONS': {
            'MAX_ENTRIES': 300,
        },
    },
}

# ---------------------------------------------------------------------------
# Password validation (kept minimal for API-only project)
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
STATIC_URL = '/static/'
STATICFILES_DIRS = []
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ---------------------------------------------------------------------------
# Default primary key field type
# ---------------------------------------------------------------------------
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/minute',
    },
}

# ---------------------------------------------------------------------------
# CORS — Allow all origins in development
# ---------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = True  # Restrict in production

# ---------------------------------------------------------------------------
# Fuel Route Planner — Application Constants
# ---------------------------------------------------------------------------

# Path to the OPIS fuel prices CSV file
FUEL_PRICES_CSV = BASE_DIR / 'data' / 'fuel_prices.csv'

# Path to the preprocessed geocoded stations cache (generated by management command)
STATIONS_GEOCODED_JSON = BASE_DIR / 'data' / 'stations_geocoded.json'

# Vehicle constants (as per assignment)
VEHICLE_TANK_MILES = 500        # Maximum range on a full tank (miles)
VEHICLE_MPG = 10                # Fuel efficiency (miles per gallon)
VEHICLE_TANK_GALLONS = 50       # = VEHICLE_TANK_MILES / VEHICLE_MPG

# Safety reserve — never plan a leg that empties the tank (road detours, etc.)
# Source: Ali Nasir case study; FMCSA best practices
SAFETY_RESERVE_MILES = 30       # Keep at least 30 miles of fuel in reserve
EFFECTIVE_RANGE_MILES = VEHICLE_TANK_MILES - SAFETY_RESERVE_MILES  # 470 miles

# Truck stop average dwell time (minutes)
# Source: ATRI, r/Truckers, Pilot Flying J operations data
DEFAULT_STOP_MINUTES = 20       # Average fuel stop dwell time

# Supported fuel strategies (first one is the default)
FUEL_STRATEGIES = ('full_tank', 'exact_gallon')
DEFAULT_FUEL_STRATEGY = 'full_tank'
MAX_STOP_MINUTES = 240          # Upper bound for the stop_minutes query param

# OSRM routing API (public demo server — no key, fair-use policy)
OSRM_BASE_URL = 'https://router.project-osrm.org'
OSRM_TIMEOUT_SECONDS = 10

# Optional OpenRouteService fallback (free key from openrouteservice.org).
# Only used when OSRM fails and a key is configured.
OPENROUTESERVICE_BASE_URL = 'https://api.openrouteservice.org'
OPENROUTESERVICE_API_KEY = os.environ.get('OPENROUTESERVICE_API_KEY', '')

# How long a computed route stays cached (seconds)
ROUTE_CACHE_SECONDS = 7 * 86400  # roads change slowly

# Corridor search: stations within this road-perpendicular distance count as
# "on the route". Distances are computed on the sphere (not raw degrees), so
# 10 miles means 10 miles at every latitude.
CORRIDOR_RADIUS_MILES = 10
CORRIDOR_SAMPLE_INTERVAL_MILES = 5       # Spacing of route sample points
ROUTE_PROJECTION_RESOLUTION_MILES = 0.25  # Mile-marker precision

# Geometry returned to the browser is simplified (Douglas-Peucker) to keep the
# response small. ~0.005 deg is roughly 0.3 miles.
ROUTE_GEOMETRY_SIMPLIFY_TOLERANCE_DEG = 0.005

# Geocoding
GEOCODING_TIMEOUT_SECONDS = 5
GEOCODE_CACHE_SECONDS = 30 * 86400
NOMINATIM_USER_AGENT = os.environ.get(
    'NOMINATIM_USER_AGENT', 'spotter-fuel-route-planner/1.0'
)
NOMINATIM_MIN_DELAY_SECONDS = 1.1  # Nominatim policy: max 1 request/second

# Offline US place gazetteer (US Census Bureau, public domain). Downloaded once
# by `manage.py geocode_stations`, used for stations AND for start/finish input.
GAZETTEER_BASE_URL = 'https://www2.census.gov/geo/docs/maps-data/data/gazetteer'
GAZETTEER_YEAR = 2024
GAZETTEER_DIR = BASE_DIR / 'data' / 'gazetteer'
US_PLACES_JSON = BASE_DIR / 'data' / 'us_places.json'
# Persisted Nominatim answers for station cities the gazetteer misses,
# so re-running the geocode command never re-queries the network.
GEOCODE_FALLBACK_CACHE_JSON = BASE_DIR / 'data' / 'geocode_fallback_cache.json'

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'router_app': {'handlers': ['console'], 'level': 'INFO'},
    },
}
