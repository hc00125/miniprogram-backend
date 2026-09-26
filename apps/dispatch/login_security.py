import hashlib
import hmac
import time
from django.conf import settings
from django.db import transaction
from .models import LoginThrottle


@transaction.atomic
def allow_login_attempt(request):
    window = int(time.time()) // 900
    # Nginx overwrites X-Real-IP; do not trust the client-supplied X-Forwarded-For chain.
    ip = request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR', '')
    username = str(request.POST.get('username', '')).strip().casefold()[:150]
    buckets = [(f'account:{username}', 10), (f'ip:{ip}', 60)]
    for raw, limit in sorted(buckets):
        key = hmac.new(settings.SECRET_KEY.encode(), f'dispatch-login:{window}:{raw}'.encode(), hashlib.sha256).hexdigest()
        LoginThrottle.objects.get_or_create(key=key, defaults={'window': window})
        bucket = LoginThrottle.objects.select_for_update().get(key=key)
        if bucket.hits >= limit:
            return False
        bucket.hits += 1
        bucket.save(update_fields=['hits'])
    LoginThrottle.objects.filter(window__lt=window-192).delete()
    return True
