"""Navigation only, never order acceptance. Missing business adapter fails closed."""
import hashlib
import secrets
from urllib.parse import urlparse
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string
from .models import KookEntryIntent, KookBinding

SAFE_TARGET='pages/player/grab/index'

def safe_url_link(link):
    try:
        parsed = urlparse(link)
        return bool(isinstance(link, str) and len(link) <= 1000 and
            parsed.scheme == 'https' and parsed.hostname == 'wxaurl.cn' and
            not parsed.username and not parsed.password and not parsed.port and
            not parsed.query and not parsed.fragment and parsed.path not in {'', '/'})
    except (TypeError, ValueError):
        return False


def wechat_url_link(*, path, query, expires_at):
    """Fixed official API with renewable server-only stable tokens.

    Failures are navigation-only degradation; never log credential-bearing URLs.
    """
    if not getattr(settings, 'KOOK_WECHAT_URL_LINK_ENABLED', False):
        return ''
    import re
    import requests
    from .wechat_token import get_access_token
    now = timezone.now()
    if path != SAFE_TARGET or not re.fullmatch(r'kook_intent=[A-Za-z0-9_-]{16,100}', query):
        return ''
    if not now < expires_at <= now + timedelta(days=1):
        return ''
    try:
        token = get_access_token()
        session = requests.Session()
        try:
            session.trust_env = False
            for attempt in range(2):
                response = session.post('https://api.weixin.qq.com/wxa/generate_urllink',
                    params={'access_token': token}, json={'path': SAFE_TARGET, 'query': query,
                        'is_expire': True, 'expire_type': 0, 'expire_time': int(expires_at.timestamp()),
                        'env_version': 'release'}, timeout=(3, 8), allow_redirects=False)
                if response.status_code != 200:
                    return ''
                data = response.json()
                if isinstance(data, dict) and data.get('errcode') in {40001, 40014, 42001} and attempt == 0:
                    token = get_access_token(rejected_token=token)
                    continue
                break
        finally:
            session.close()
        link = data.get('url_link', '') if isinstance(data, dict) and data.get('errcode') == 0 else ''
        return link if safe_url_link(link) else ''
    except Exception:
        return ''


def intent_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def create_intent(order_id,scope,expires_at,designation_id=None,binding=None,link_provider=None):
    if scope not in {'channel','dm'} or (scope=='dm' and (not binding or not designation_id)):
        raise ValueError('Invalid intent scope')
    expires_at=min(expires_at,timezone.now()+timedelta(days=1))
    if expires_at<=timezone.now():
        raise ValueError('Intent already expired')
    token=secrets.token_urlsafe(24)
    obj=KookEntryIntent.objects.create(digest=intent_digest(token),order_id=order_id,scope=scope,designation_id=designation_id,binding=binding,binding_version=binding.version if binding else None,expires_at=expires_at)
    if link_provider:
        # Provider is an injected, separately verified WeChat adapter, not a URL
        # or credential supplied by API callers. No default external request.
        try:
            link=link_provider(path=SAFE_TARGET,query='kook_intent='+token,expires_at=expires_at)
            if safe_url_link(link):
                obj.url_link=link
                obj.save(update_fields=['url_link'])
        except Exception:
            # Do not print provider exceptions: WeChat URLs may carry credentials.
            pass
    return token,obj

def delivery_url_link(delivery):
    """Worker-only, after claim commit; cached navigation is never authorization."""
    if not getattr(settings, 'KOOK_WECHAT_URL_LINK_ENABLED', False) or delivery.event.event_type not in {
            'public_slots.opened', 'public_slots.changed', 'replacement.opened', 'designation.created'}:
        return ''
    binding = delivery.binding if delivery.binding_id else None
    cached = KookEntryIntent.objects.filter(order_id=delivery.event.order_id, scope=delivery.scope,
        designation_id=delivery.event.designation_id, binding_id=delivery.binding_id,
        binding_version=delivery.binding_version, revoked_at__isnull=True,
        expires_at__gt=timezone.now()+timedelta(minutes=1)).exclude(url_link='').order_by('-expires_at').first()
    if cached and safe_url_link(cached.url_link):
        return cached.url_link
    _, intent = create_intent(delivery.event.order_id, delivery.scope, delivery.event.expires_at,
        designation_id=delivery.event.designation_id, binding=binding, link_provider=wechat_url_link)
    return intent.url_link


def resolve_intent(token,player):
    result={'state':'expired','safe_target':SAFE_TARGET,'expires_at':None}
    if not isinstance(token,str) or not 16<=len(token)<=100:
        return result
    obj=KookEntryIntent.objects.filter(digest=intent_digest(token)).first()
    if not obj:
        return result
    result['expires_at']=obj.expires_at
    if obj.revoked_at or obj.expires_at<=timezone.now():
        return result
    result['state']='forbidden'
    if obj.scope=='dm' and not KookBinding.objects.filter(pk=obj.binding_id,player=player,active=True,version=obj.binding_version).exists():
        return result
    resolver=getattr(settings,'KOOK_ENTRY_RESOLVER','')
    if not resolver or player is None:
        return result
    resolver=import_string(resolver) if isinstance(resolver,str) else resolver
    facts=resolver(obj,player)
    if not isinstance(facts,dict) or facts.get('state') not in {'available','invited','joined','closed','forbidden','expired'}:
        return result
    result['state']=facts['state']
    if result['state'] not in {'forbidden','expired'} and isinstance(facts.get('order_no'),str):
        result['order_no']=facts['order_no'][:80]
    return result
