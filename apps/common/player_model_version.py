"""Model-version compatibility, never database-error recovery.

Legacy release models have no archive field. Future models must read their real
field; a missing database migration is deliberately not caught or suppressed.
"""
from django.core.exceptions import FieldDoesNotExist


def archive_filter(model):
    try:
        model._meta.get_field('is_archived')
    except FieldDoesNotExist:
        return {}
    return {'is_archived': False}


def is_archived(player):
    return bool(player.is_archived) if archive_filter(type(player)) else False
