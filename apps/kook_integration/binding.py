import hashlib
import hmac
import secrets
from datetime import timedelta
from django.db import transaction, IntegrityError
from django.utils import timezone
from apps.players.models import Player
from .models import KookBinding, KookBindingChallenge, KookDelivery, KookEntryIntent, KookAuditLog
from .secrets import secret, bot_key, require_enabled, KookError

ACTIVE_STATES = ['pending', 'awaiting_confirmation']

def digest(value):
    return hmac.new(secret('binding_pepper').encode(), value.encode(), hashlib.sha256).hexdigest()

def confirmation_nonce(challenge):
    return digest('confirm:' + str(challenge.pk) + ':' + challenge.candidate_kook_user_id + ':' + challenge.proof_event_id)

def masked(value):
    return ('***' + value[-4:]) if value else None

def cancel_deliveries(binding):
    KookDelivery.objects.filter(binding=binding, status='queued').update(status='cancelled', last_error_code='BINDING_REVOKED')
    KookEntryIntent.objects.filter(binding=binding, revoked_at__isnull=True).update(revoked_at=timezone.now())

@transaction.atomic
def create_challenge(player, purpose):
    require_enabled()
    Player.objects.select_for_update().get(pk=player.pk)
    if purpose not in {'bind', 'rebind'}:
        raise KookError('INVALID_PURPOSE', 400)
    if purpose == 'bind' and KookBinding.objects.filter(bot_key=bot_key(), player=player, active=True).exists():
        raise KookError('ALREADY_BOUND')
    recent = KookBindingChallenge.objects.filter(player=player, bot_key=bot_key(), created_at__gte=timezone.now()-timedelta(hours=1))
    if recent.count() >= 5:
        raise KookError('CHALLENGE_RATE_LIMIT', 429)
    recent.model.objects.filter(player=player, bot_key=bot_key(), state__in=ACTIVE_STATES).update(state='cancelled')
    code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(10))
    challenge = KookBindingChallenge.objects.create(player=player, bot_key=bot_key(), purpose=purpose, digest=digest(code), expires_at=timezone.now()+timedelta(minutes=10))
    return challenge, code

@transaction.atomic
def claim_code(code, author_id, event_id, display_name=''):
    challenge = KookBindingChallenge.objects.select_for_update().filter(bot_key=bot_key(), digest=digest(code)).first()
    if not challenge or challenge.expires_at <= timezone.now() or challenge.state != 'pending':
        return False
    challenge.state = 'awaiting_confirmation'
    challenge.candidate_kook_user_id = author_id
    challenge.kook_display_name = display_name[:100]
    challenge.proof_event_id = event_id
    challenge.confirmation_nonce_digest = digest(confirmation_nonce(challenge))
    challenge.save()
    return True

def owned_challenge(player, pk, lock=False):
    query = KookBindingChallenge.objects
    if lock:
        query = query.select_for_update()
    challenge = query.filter(pk=pk, player=player, bot_key=bot_key()).first()
    if not challenge:
        raise KookError('NOT_FOUND', 404)
    return challenge

def confirm(player, pk, nonce, enabled):
    # Keep attributable failure counters even when the API returns an error.
    rejected = False
    with transaction.atomic():
        Player.objects.select_for_update().get(pk=player.pk)
        challenge = owned_challenge(player, pk, True)
        if challenge.state in ACTIVE_STATES and not hmac.compare_digest(digest(nonce), challenge.confirmation_nonce_digest):
            challenge.attempts += 1
            if challenge.attempts >= 5:
                challenge.state = 'cancelled'
            challenge.save(update_fields=['attempts','state'])
            rejected = True
        elif challenge.state == 'cancelled':
            rejected = True
        else:
            return _confirm(player, pk, nonce, enabled)
    if rejected:
        raise KookError('CONFIRMATION_INVALID')

@transaction.atomic
def _confirm(player, pk, nonce, enabled):
    require_enabled()
    Player.objects.select_for_update().get(pk=player.pk)
    challenge = owned_challenge(player, pk, True)
    if not hmac.compare_digest(digest(nonce), challenge.confirmation_nonce_digest):
        raise KookError('CONFIRMATION_INVALID')
    if challenge.state == 'confirmed':
        if challenge.binding and challenge.binding.active:
            return challenge.binding
        raise KookError('BINDING_REVOKED')
    if challenge.expires_at <= timezone.now():
        raise KookError('CHALLENGE_EXPIRED', 410)
    if challenge.state != 'awaiting_confirmation':
        raise KookError('CHALLENGE_STATE_CONFLICT')
    try:
        with transaction.atomic():
            old = KookBinding.objects.select_for_update().filter(bot_key=bot_key(), player=player, active=True).first()
            if old:
                old.active = False
                old.revoked_at = timezone.now()
                old.save(update_fields=['active', 'revoked_at'])
                cancel_deliveries(old)
            result = KookBinding.objects.create(bot_key=bot_key(), player=player, kook_user_id=challenge.candidate_kook_user_id, display_name=challenge.kook_display_name, notifications_enabled=enabled)
    except IntegrityError:
        raise KookError('KOOK_ACCOUNT_CONFLICT')
    challenge.state = 'confirmed'
    challenge.binding = result
    challenge.consumed_at = timezone.now()
    challenge.save()
    KookAuditLog.objects.create(actor=str(player.pk), action='binding.confirm', object_id=str(result.pk), result='bound')
    return result

@transaction.atomic
def unbind(player, version):
    Player.objects.select_for_update().get(pk=player.pk)
    current = KookBinding.objects.select_for_update().filter(bot_key=bot_key(), player=player, active=True).first()
    if not current:
        return
    if str(current.version) != str(version):
        raise KookError('BINDING_VERSION_CONFLICT')
    current.active = False
    current.revoked_at = timezone.now()
    current.save(update_fields=['active','revoked_at'])
    cancel_deliveries(current)
    KookBindingChallenge.objects.filter(player=player, bot_key=bot_key(), state__in=ACTIVE_STATES).update(state='cancelled')
    KookAuditLog.objects.create(actor=str(player.pk), action='binding.revoke', object_id=str(current.pk), result='revoked')
