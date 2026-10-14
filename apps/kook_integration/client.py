"""KOOK fixed-host transport. No exactly-once claim for create requests."""
from dataclasses import dataclass
import math
import requests
from django.conf import settings
from .secrets import secret, bot_key

@dataclass(frozen=True)
class SendResult:
    status: str
    error_code: str = ''
    message_id: str = ''
    retry_after: float = 0
    bucket: str = ''
    global_limit: bool = False
    reset_unverified: bool = False

def rate_headers(response):
    """Official /doc/rate-limit defines Reset as a wait, but supplies NO unit.

    Never infer milliseconds from value size. Until the unit is independently
    verified/configured, an exhausted Reset-only response is a manual hold.
    Retry-After follows HTTP seconds/date semantics, not KOOK Reset semantics.
    """
    headers = requests.structures.CaseInsensitiveDict(response.headers)
    exhausted = response.status_code == 429 or str(headers.get('X-Rate-Limit-Remaining', '')).strip() == '0'
    if not exhausted:
        return {}
    global_limit = 'X-Rate-Limit-Global' in headers and str(headers['X-Rate-Limit-Global']).strip().lower() not in {'false', '0'}
    unverified = False
    try:
        if headers.get('Retry-After'):
            raw = headers['Retry-After']
            try:
                delay = float(raw)
            except ValueError:
                from email.utils import parsedate_to_datetime
                from django.utils import timezone
                delay = max(0, (parsedate_to_datetime(raw) - timezone.now()).total_seconds())
        else:
            unit = getattr(settings, 'KOOK_RATE_RESET_UNIT', 'unverified')
            if unit not in {'seconds', 'milliseconds'}:
                raise ValueError('Reset unit unverified')
            delay = float(headers['X-Rate-Limit-Reset'])
            if unit == 'milliseconds':
                delay /= 1000
        # Overflow/malformed headers must not crash the worker or shorten a hold.
        if not math.isfinite(delay) or delay < 0 or delay > 86400 * 365:
            raise ValueError('Invalid reset delay')
    except (KeyError, TypeError, ValueError, OverflowError):
        delay, unverified = 60, True
    return {'retry_after': max(1, delay), 'bucket': str(headers.get('X-Rate-Limit-Bucket', '')),
            'global_limit': global_limit, 'reset_unverified': unverified}


class KookClient:
    BASE = 'https://www.kookapp.cn/api/v3/'

    def __init__(self, token=None, transport=None):
        self.bot_key = bot_key()  # Snapshot paired with this credential, never live settings.
        self.token = token if token is not None else secret('bot_token')
        self.transport = transport or requests.Session()
        if transport is None:
            self.transport.trust_env = False

    def inspect_target(self, guild_id, channel_id):
        # Only fixed read-only paths; no writes, redirects or auto-configuration.
        values=[]
        for path,params in [('user/me',{}),('channel/view',{'target_id':channel_id})]:
            try:
                response=self.transport.get(self.BASE+path,params=params,headers={'Authorization':'Bot '+self.token},timeout=(3,8),allow_redirects=False)
                body=response.json()
                if response.status_code!=200 or body.get('code')!=0 or not isinstance(body.get('data'),dict):
                    raise ValueError()
                values.append(body['data'])
            except (requests.RequestException,ValueError,AttributeError,TypeError):
                from .secrets import KookError
                raise KookError('TARGET_INSPECTION_FAILED',503)
        identity,channel=values
        return {'identity_ok':bool(identity.get('id') and identity.get('bot') is True), 'channel_ok':str(channel.get('id'))==channel_id and str(channel.get('guild_id'))==guild_id and channel.get('type')==1, 'permissions_verified':False}

    def send(self, scope, target_id, content, operation='create', message_id=''):
        if scope not in {'channel','dm'} or operation not in {'create','update'}:
            raise ValueError('Unsupported operation')
        if operation == 'update' and not message_id:
            raise ValueError('Known message ID required')
        path = ('message/' if scope == 'channel' else 'direct-message/') + operation
        payload = {'content':content}
        if operation == 'create':
            payload.update(type=10,target_id=target_id)
        else:
            payload['msg_id']=message_id
        try:
            response=self.transport.post(self.BASE+path,json=payload,headers={'Authorization':'Bot '+self.token},timeout=(3,8),allow_redirects=False)
        except requests.RequestException:
            return SendResult('unknown','TRANSPORT_UNKNOWN')
        rate = rate_headers(response)
        def result(status, error_code='', message_id=''):
            return SendResult(status, error_code, message_id=message_id, **rate)
        if response.status_code == 429:
            return result('retry', 'RATE_LIMITED')
        if response.status_code >= 500:
            return result('unknown','UPSTREAM_UNKNOWN')
        if response.status_code != 200:
            return result('failed','HTTP_'+str(response.status_code))
        try:
            body=response.json()
            if not isinstance(body,dict):
                raise ValueError()
            if body.get('code') != 0:
                return result('failed','KOOK_BUSINESS_REJECTED')
            msg_id=(body.get('data') or {}).get('msg_id') or (message_id if operation=='update' else '')
            if not isinstance(msg_id,str) or not 0 < len(msg_id) <= 100:
                return result('unknown','MISSING_MESSAGE_ID')
            return result('sent',message_id=msg_id)
        except (ValueError,AttributeError,TypeError):
            return result('unknown','INVALID_RESPONSE')
