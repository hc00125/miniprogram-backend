"""Server-only stable tokens; share legacy cache, NEVER invalidate other consumers.

One host only: a private systemd RuntimeDirectory provides the process lock.
Legacy consumers do not take this lock; normal stable_token mode is essential.
No exception detail or credential-bearing HTTP data is logged here.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache

_lock = threading.Lock()


def _credential(name):
    if getattr(settings, 'KOOK_TESTING', False):
        value = settings.KOOK_TEST_SECRETS.get(name, '')
    else:
        directory = os.environ.get('CREDENTIALS_DIRECTORY', '')
        value = (Path(directory) / ('kook_' + name)).read_text().strip() if directory else ''
    if not value:
        raise RuntimeError('WeChat credential unavailable')
    return value


def get_access_token(*, rejected_token=None):
    appid = _credential('wechat_app_id')
    configured = getattr(settings, 'WECHAT_APP_ID', '')
    if configured and configured != appid:
        raise RuntimeError('WeChat application mismatch')
    key = 'wechat-stable-access-token:' + appid
    with _lock:
        token = cache.get(key)
        if isinstance(token, str) and token and token != rejected_token:
            return token
        secret = _credential('wechat_app_secret')
        directory = getattr(settings, 'KOOK_WECHAT_TOKEN_LOCK_DIR', '')
        if not directory or not Path(directory).is_absolute():
            raise RuntimeError('WeChat private runtime directory required')
        path = Path(directory) / ('wechat-' + hashlib.sha256(appid.encode()).hexdigest() + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('WeChat token busy') from None
                    time.sleep(0.02)
            token = cache.get(key)
            if isinstance(token, str) and token and token != rejected_token:
                return token
            session = requests.Session()
            try:
                session.trust_env = False
                response = session.post('https://api.weixin.qq.com/cgi-bin/stable_token',
                    json={'grant_type': 'client_credential', 'appid': appid,
                          'secret': secret, 'force_refresh': False},
                    timeout=(3, 8), allow_redirects=False)
                if response.status_code != 200:
                    raise ValueError('Token response rejected')
                data = response.json()
                token = data.get('access_token') if isinstance(data, dict) else None
                expires = data.get('expires_in') if isinstance(data, dict) else None
                if (not isinstance(token, str) or not token or len(token) > 4096 or
                        type(expires) is not int or not 300 < expires <= 7200 or data.get('errcode', 0) != 0):
                    raise ValueError('Token response rejected')
                if token == rejected_token:
                    raise ValueError('Rejected token remains current')
                cache.set(key, token, expires - 300)
                return token
            except Exception:
                raise RuntimeError('WeChat token unavailable') from None
            finally:
                session.close()
        finally:
            os.close(fd)
