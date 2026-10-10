"""Append-only receipt: deleting it must never reopen one-time income."""
from django.db import migrations

FORWARD_SQL = """
CREATE FUNCTION patronage_preserve_income_receipt() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Patronage income receipt is immutable' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER patronage_income_receipt_immutable
BEFORE UPDATE OR DELETE ON patronage_patronageearning
FOR EACH ROW EXECUTE FUNCTION patronage_preserve_income_receipt();
"""
REVERSE_SQL = """
DROP TRIGGER IF EXISTS patronage_income_receipt_immutable ON patronage_patronageearning;
DROP FUNCTION IF EXISTS patronage_preserve_income_receipt();
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD_SQL)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [('patronage', '0007_purchase_attempt_binding')]
    operations = [migrations.RunPython(forwards, backwards)]
