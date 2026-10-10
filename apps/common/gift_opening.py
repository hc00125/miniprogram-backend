"""Transaction lock shared by gift listing and recipient eligibility writes.

No SQL at import/startup. Acquire BEFORE recipient/gift row locks. Not a scanner.
Applications which do not install gifts keep their previous behavior.
"""
from contextlib import contextmanager
from django.apps import apps
from django.db import connections, transaction


@contextmanager
def gift_opening_write(using='default'):
    with transaction.atomic(using=using):
        if apps.is_installed('apps.gifts'):
            connection = connections[using]
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [714203, 1])
        yield
