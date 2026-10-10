"""Authenticated, bounded, durable ACK only; no KOOK HTTP in this process."""
import base64
import hashlib
import hmac
import json
import re
import zlib
from django.conf import settings
from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from .models import KookInboundEvent
from .secrets import secret, bot_key, KookError

RAW_LIMIT = 256 * 1024
PLAIN_LIMIT = 1024 * 1024
COMMAND = re.compile(r'bind ([A-HJ-NP-Z2-9]{10})\Z')

def decode_body(raw):
    if len(raw) > RAW_LIMIT:
        raise KookError('PAYLOAD_TOO_LARGE', 413)
    if not raw.lstrip().startswith(b'{'):
        decoder = zlib.decompressobj()
        raw = decoder.decompress(raw, PLAIN_LIMIT + 1)
        if len(raw) > PLAIN_LIMIT or decoder.unconsumed_tail:
            raise KookError('PAYLOAD_TOO_LARGE', 413)
        if not decoder.eof or decoder.unused_data:
            raise ValueError('Invalid compressed body')
    outer = json.loads(raw)
    if getattr(settings, 'KOOK_ENCRYPTION_ENABLED', False):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        key = secret('encrypt_key').encode()
        if len(key) > 32:
            raise KookError('KOOK_CONFIGURATION_MISSING', 503)
        data = base64.b64decode(outer['encrypt'], validate=True)
        iv, encoded = data[:16], data[16:]
        ciphertext = base64.b64decode(encoded, validate=True)
        decryptor = Cipher(algorithms.AES(key.ljust(32, b'\0')), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpad = PKCS7(128).unpadder()
        raw = unpad.update(padded) + unpad.finalize()
        outer = json.loads(raw)
    elif 'encrypt' in outer:
        raise ValueError('Unexpected ciphertext')
    if not isinstance(outer, dict) or outer.get('s') != 0 or not isinstance(outer.get('d'), dict):
        raise ValueError('Invalid envelope')
    return outer

@csrf_exempt
def webhook(request):
    if request.method != 'POST':
        return JsonResponse({'code':'METHOD_NOT_ALLOWED'}, status=405)
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > RAW_LIMIT:
            raise KookError('PAYLOAD_TOO_LARGE', 413)
        envelope = decode_body(request.body)
        d = envelope['d']
        supplied = d.get('verify_token')
        expected = secret('verify_token')
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise KookError('WEBHOOK_FORBIDDEN', 403)
        if d.get('channel_type') == 'WEBHOOK_CHALLENGE' and d.get('type') == 255:
            if not isinstance(d.get('challenge'), str):
                raise ValueError('Invalid challenge')
            return JsonResponse({'challenge':d['challenge']})
        if not getattr(settings, 'KOOK_ENABLED', False):
            return JsonResponse({})
        extra = d.get('extra') or {}
        if not isinstance(extra, dict):
            raise ValueError('Invalid extra')
        author = extra.get('author') or {}
        if not isinstance(author, dict):
            raise ValueError('Invalid author')
        # KOOK clients also send plain-looking private commands as KMarkdown (9).
        # Keep exact command matching below; never interpret/strip rich formatting.
        if d.get('channel_type') != 'PERSON' or d.get('type') not in (1, 9) or author.get('bot') is not False:
            return JsonResponse({})
        match = COMMAND.fullmatch(d.get('content', ''))
        author_id = d.get('author_id')
        if not match or not isinstance(author_id, str) or not 0 < len(author_id) <= 80:
            return JsonResponse({})
        allowed = getattr(settings, 'KOOK_BINDING_USER_ALLOWLIST', None)
        if allowed is not None and author_id not in allowed:
            return JsonResponse({})
        payload = {'code': match.group(1), 'author_id':author_id, 'display_name':str(author.get('username',''))[:100]}
        stable = d.get('msg_id')
        if stable:
            key = hashlib.sha256(('PERSON:1:' + str(stable)).encode()).hexdigest()
        else:
            # 10-minute receive window, sn is never globally unique.
            canonical = json.dumps({'d':d,'sn':envelope.get('sn'), 'window':int(timezone.now().timestamp())//600}, sort_keys=True, separators=(',',':'))
            key = hashlib.sha256(canonical.encode()).hexdigest()
        KookInboundEvent.objects.get_or_create(bot_key=bot_key(), event_key=key, defaults={'sn':int(envelope.get('sn') or 0), 'event_time':int(d.get('msg_timestamp') or 0), 'payload':payload})
        return JsonResponse({})
    except KookError as exc:
        return JsonResponse({'code':str(exc.detail)}, status=exc.status_code)
    except DatabaseError:
        return JsonResponse({'code':'INBOUND_STORAGE_UNAVAILABLE'}, status=503)
    except (ValueError, TypeError, KeyError, zlib.error, UnicodeError):
        return JsonResponse({'code':'INVALID_WEBHOOK'}, status=400)
