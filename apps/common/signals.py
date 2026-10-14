from django.db.models.signals import pre_save
from django.dispatch import receiver

from apps.players.models import Player

from .media_security import ensure_media_publishable


@receiver(pre_save, sender=Player)
def require_safe_player_audio_before_publish(sender, instance, **kwargs):
    """Last-line guard: a new public audio URL can only be saved after WeChat returns pass.

    Existing legacy audio is left untouched when a Player row is saved for unrelated fields.
    Any new/changed non-empty URL, including changes made through Django Admin, is checked.
    """
    del sender, kwargs
    audio_url = str(instance.audio_intro_url or '').strip()
    if not audio_url:
        return

    if instance.pk:
        previous_audio = (
            Player.objects
            .filter(pk=instance.pk)
            .values_list('audio_intro_url', flat=True)
            .first()
        )
        if (previous_audio or '') == audio_url:
            return

    ensure_media_publishable(user=instance.user, media_url=audio_url)
