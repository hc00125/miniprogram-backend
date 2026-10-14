"""Account eligibility events only; no startup queries or asynchronous scans.

Model/admin saves are covered; QuerySet.update/bulk_update/raw SQL deliberately
are not signals. Existing gift recipient checks remain authoritative and fail
closed when a configuration is absent. Admin changes already run in atomic().
"""
from django.contrib.auth import get_user_model
from django.db.models.signals import pre_save, post_save
from apps.accounts.models import ClientProfile
from apps.players.models import Player
from .services.auto_open import open_missing


def before_account_save(sender, instance, raw=False, using='default', update_fields=None, **kwargs):
    if raw:
        return
    fields = {'is_active'} if sender is get_user_model() else {'account_status', 'user_id'}
    instance._gift_eligibility_changed = False
    if update_fields is not None and not (set(update_fields) & (fields | {'user'})):
        return
    old = sender.objects.using(using).filter(pk=instance.pk).values(*fields).first() if instance.pk else None
    instance._gift_eligibility_changed = old is None or any(old[f] != getattr(instance, f) for f in fields)


def after_account_save(sender, instance, raw=False, using='default', **kwargs):
    if raw or not getattr(instance, '_gift_eligibility_changed', False):
        return
    if using != 'default':
        raise ValueError('Gift auto-opening supports only the configured default database')
    user_id = instance.pk if sender is get_user_model() else instance.user_id
    for player_id in Player.objects.filter(user_id=user_id).values_list('pk', flat=True):
        open_missing(player_id=player_id)


def connect():
    for sender in (get_user_model(), ClientProfile):
        label = sender._meta.label_lower
        pre_save.connect(before_account_save, sender=sender, dispatch_uid='gift-auto-before-' + label)
        post_save.connect(after_account_save, sender=sender, dispatch_uid='gift-auto-after-' + label)
