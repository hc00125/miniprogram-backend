from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute("""
    CREATE FUNCTION wallet_spend_immutable() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'SPEND_AUDIT_IMMUTABLE' USING ERRCODE='23514';
      END IF;
      IF (to_jsonb(NEW) - ARRAY['status','reserved_amount','evidence','blocker','updated_at'])
          IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['status','reserved_amount','evidence','blocker','updated_at']) THEN
        RAISE EXCEPTION 'SPEND_REQUEST_IMMUTABLE' USING ERRCODE='23514';
      END IF;
      IF OLD.status IN ('completed','failed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'SPEND_TERMINAL_IMMUTABLE' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER wallet_spend_immutable BEFORE UPDATE OR DELETE ON wallet_walletspendattempt
    FOR EACH ROW EXECUTE FUNCTION wallet_spend_immutable();
    """)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER wallet_spend_immutable ON wallet_walletspendattempt; DROP FUNCTION wallet_spend_immutable();')


class Migration(migrations.Migration):
    dependencies = [('wallet', '0010_walletspendattempt_walletspendaudit_and_more')]
    operations = [migrations.RunPython(install, uninstall)]
