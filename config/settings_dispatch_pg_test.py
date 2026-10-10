from .settings_surcharge_v2_pg_test import *  # noqa
ROOT_URLCONF = 'config.urls'
MIDDLEWARE = ['apps.dispatch.middleware.ConsoleCookieSecurity'] + list(MIDDLEWARE) + ['django.middleware.csrf.CsrfViewMiddleware']
