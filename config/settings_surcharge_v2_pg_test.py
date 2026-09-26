"""Task-only disposable PostgreSQL; no production config, TCP or credentials."""
from .settings_surcharge_v2_test import *  # noqa
from pathlib import Path
_PG = Path('/root/.hermes/cache/scratch/touchi-commerce-multiagent-20260923/backend-pg')
if not (_PG / 'data' / 'PG_VERSION').is_file():
    raise RuntimeError('Task PostgreSQL cluster has not been initialized')
DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql',
    'NAME': 'postgres', 'USER': 'touchi_backend_v2', 'HOST': str(_PG), 'PORT': '55459',
    'TEST': {'NAME': 'test_touchi_backend_v2', 'TEMPLATE': 'template0', 'CHARSET': 'UTF8'}}}
