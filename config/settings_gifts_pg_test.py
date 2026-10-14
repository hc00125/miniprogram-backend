"""Disposable scratch PostgreSQL only; never production settings or TCP."""
from .settings_gifts_test import *  # noqa: F401,F403
from pathlib import Path

_PG = Path('/root/.hermes/cache/scratch/gift-foundation-pg')
if not (_PG / 'data' / 'PG_VERSION').is_file():
    raise RuntimeError('Start the isolated scratch PostgreSQL cluster first')
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.postgresql', 'NAME': 'postgres', 'USER': 'postgres',
    'HOST': str(_PG), 'PORT': '55449',
    'TEST': {'NAME': 'test_gifts_foundation', 'TEMPLATE': 'template0', 'CHARSET': 'UTF8'},
}}
