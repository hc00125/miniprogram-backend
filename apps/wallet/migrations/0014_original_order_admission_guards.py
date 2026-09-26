"""Protect active legacy payment intents and mapped-order SQL writes.

No historical Payment/ledger/paid status is rewritten. Existing unpaid orders
still awaiting payment may have dispatched the old non-durable call. Closed
history is not promoted into new account-wide restrictions by this release.
Persisted attempts/bindings, including real unknowns, are never cleared.
"""
from django.db import migrations



def quarantine_legacy(apps, schema_editor):
    alias = schema_editor.connection.alias
    Recharge = apps.get_model('wallet', 'RechargeOrder')
    Order = apps.get_model('orders', 'Order')
    Binding = apps.get_model('wallet', 'OrderWalletSpend')
    sources = {}
    for r in Recharge.objects.using(alias).filter(status='credited').select_related('profile').iterator():
        if (r.notify_payload or {}).get('mode') == 'short_series_coin':
            sources.setdefault(r.profile.user_id, []).append(r.recharge_no)
    for order in Order.objects.using(alias).filter(paid=False, boss_user_id__in=sources,
            status='待支付').iterator():
        # Scope admission to still-active original payment intents. This does
        # not assert a remote result for closed orders or mutate their finances.
        if Binding.objects.using(alias).filter(order_id=order.pk).exists():
            continue
        if apps.get_model('orders', 'OrderCheckout').objects.using(alias).filter(order_id=order.pk, attempt__isnull=False).exists():
            continue
        Binding.objects.using(alias).create(order_id=order.pk, blocker='LEGACY_COIN_REVIEW_REQUIRED',
            intent={'version': 'pre-durable-admission-v1', 'order_no': order.order_no,
                'boss_id': order.boss_user_id, 'prior_status': order.status,
                'coin_recharge_nos': sources[order.boss_user_id],
                'reason': 'No per-spend proof; never synthesize failed or re-debit'})


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute("""
    CREATE FUNCTION wallet_original_order_guard() RETURNS trigger AS $$
    DECLARE state text; blocked text;
    BEGIN
      SELECT a.status, b.blocker INTO state, blocked
      FROM wallet_orderwalletspend b LEFT JOIN wallet_walletspendattempt a ON a.id=b.attempt_id
      WHERE b.order_id=OLD.id;
      IF NOT FOUND THEN RETURN NEW; END IF;
      IF (NEW.status IS DISTINCT FROM OLD.status OR NEW.paid IS DISTINCT FROM OLD.paid
          OR NEW.boss_user_id IS DISTINCT FROM OLD.boss_user_id
          OR NEW.total_amount IS DISTINCT FROM OLD.total_amount
          OR ROW(NEW.package_id, NEW.spec_id, NEW.required_players, NEW.fulfillment_mode,
                 NEW.order_type, NEW.parent_order_id, NEW.target_player_id, NEW.booked_hours, NEW.designated_players)
             IS DISTINCT FROM
             ROW(OLD.package_id, OLD.spec_id, OLD.required_players, OLD.fulfillment_mode,
                 OLD.order_type, OLD.parent_order_id, OLD.target_player_id, OLD.booked_hours, OLD.designated_players)) THEN
        IF COALESCE(blocked,'') <> '' THEN
          RAISE EXCEPTION 'LEGACY_COIN_REVIEW_REQUIRED' USING ERRCODE='23514';
        END IF;
        IF state IN ('dispatching','unknown','succeeded') OR
           (state='prepared' AND (NEW.paid OR NEW.status='已取消')) THEN
          RAISE EXCEPTION 'ORDER_PAYMENT_PENDING' USING ERRCODE='23514';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER wallet_original_order_guard BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION wallet_original_order_guard();

    CREATE FUNCTION wallet_original_binding_immutable() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'ORDER_SPEND_AUDIT_IMMUTABLE' USING ERRCODE='23514';
      END IF;
      IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'ORDER_SPEND_IDENTITY_IMMUTABLE' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER wallet_original_binding_immutable BEFORE UPDATE OR DELETE ON wallet_orderwalletspend
    FOR EACH ROW EXECUTE FUNCTION wallet_original_binding_immutable();
    """)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER wallet_original_order_guard ON orders; DROP FUNCTION wallet_original_order_guard(); '
            'DROP TRIGGER wallet_original_binding_immutable ON wallet_orderwalletspend; DROP FUNCTION wallet_original_binding_immutable();')


class Migration(migrations.Migration):
    dependencies = [('wallet', '0013_original_order_durable')]
    operations = [migrations.RunPython(quarantine_legacy, migrations.RunPython.noop),
                  migrations.RunPython(install, uninstall)]
