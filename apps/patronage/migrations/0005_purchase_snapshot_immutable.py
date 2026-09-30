"""Freeze purchase snapshots even for bulk SQL writers (PostgreSQL production)."""
from django.db import migrations


FORWARD_SQL = """
CREATE FUNCTION patronage_preserve_purchase_snapshot() RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['payment_status','paid_at','starts_at','expires_at','blockers'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['payment_status','paid_at','starts_at','expires_at','blockers']) THEN
        RAISE EXCEPTION 'Patronage purchase snapshot is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER patronage_purchase_snapshot_immutable
BEFORE UPDATE ON patronage_patronagepurchase
FOR EACH ROW EXECUTE FUNCTION patronage_preserve_purchase_snapshot();
"""
REVERSE_SQL = """
DROP TRIGGER IF EXISTS patronage_purchase_snapshot_immutable ON patronage_patronagepurchase;
DROP FUNCTION IF EXISTS patronage_preserve_purchase_snapshot();
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD_SQL)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [('patronage', '0004_remove_patronagepurchase_patronage_purchase_interval_and_more')]
    operations = [migrations.RunPython(forwards, backwards)]
