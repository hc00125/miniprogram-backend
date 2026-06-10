#!/bin/bash
cd /www/wwwroot/django_backend
export $(cat .env | xargs)
exec /usr/bin/python3 /usr/local/bin/gunicorn config.wsgi:application \
    --bind 0.0.0.0:8001 \
    --workers 2 \
    --timeout 120 \
    --daemon \
    --log-file /www/wwwroot/django_backend/gunicorn.log