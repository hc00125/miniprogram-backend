"""Fixed-name systemd credentials only. Never log returned values."""
import os
from pathlib import Path
from django.conf import settings
from rest_framework.exceptions import APIException

class KookError(APIException):
    def __init__(self, code, status=409):
        self.status_code = status
        super().__init__(code, code=code)

def secret(name, required=True):
    if name not in {'binding_pepper', 'verify_token', 'encrypt_key', 'bot_token', 'wechat_access_token'}:
        raise ValueError('Unknown credential')
    if getattr(settings, 'KOOK_TESTING', False):
        value = settings.KOOK_TEST_SECRETS.get(name, '')
    else:
        directory = os.environ.get('CREDENTIALS_DIRECTORY')
        try:
            value = (Path(directory) / ('kook_' + name)).read_text().strip() if directory else ''
        except OSError:
            value = ''
    if required and not value:
        raise KookError('KOOK_CONFIGURATION_MISSING', 503)
    return value

def bot_key():
    return getattr(settings, 'KOOK_BOT_KEY', 'default')

def require_enabled():
    if not getattr(settings, 'KOOK_ENABLED', False):
        raise KookError('KOOK_DISABLED', 503)
