"""V3 grant receipts preserve increments; revocation remains a separate marker."""
from django.db import migrations

FORWARD_SQL = """
CREATE FUNCTION patronage_preserve_grant_snapshot() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Patronage grant snapshot cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF (to_jsonb(NEW) - 'revoked_at') IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at') THEN
        RAISE EXCEPTION 'Patronage grant snapshot is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER patronage_grant_snapshot_immutable
BEFORE UPDATE OR DELETE ON patronage_crowngrant
FOR EACH ROW EXECUTE FUNCTION patronage_preserve_grant_snapshot();
"""
REVERSE_SQL = """
DROP TRIGGER IF EXISTS patronage_grant_snapshot_immutable ON patronage_crowngrant;
DROP FUNCTION IF EXISTS patronage_preserve_grant_snapshot();
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD_SQL)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [('patronage', '0010_crown_extension_snapshot')]
    operations = [migrations.RunPython(forwards, backwards)]
