"""Permit one-time attempt binding, retaining every existing snapshot guard."""
from importlib import import_module
from django.db import migrations

FORWARD_SQL = """
CREATE OR REPLACE FUNCTION patronage_preserve_purchase_snapshot() RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['payment_status','paid_at','starts_at','expires_at','blockers','attempt_id'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['payment_status','paid_at','starts_at','expires_at','blockers','attempt_id']) THEN
        RAISE EXCEPTION 'Patronage purchase snapshot is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.attempt_id IS DISTINCT FROM NEW.attempt_id AND
       (OLD.attempt_id IS NOT NULL OR NEW.attempt_id IS NULL OR OLD.payment_status NOT IN ('created','processing','unknown')) THEN
        RAISE EXCEPTION 'Patronage payment binding is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD_SQL)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        old = import_module('apps.patronage.migrations.0005_purchase_snapshot_immutable')
        schema_editor.execute(old.REVERSE_SQL)
        schema_editor.execute(old.FORWARD_SQL)


class Migration(migrations.Migration):
    dependencies = [('patronage', '0006_patronage_income')]
    operations = [migrations.RunPython(forwards, backwards)]
