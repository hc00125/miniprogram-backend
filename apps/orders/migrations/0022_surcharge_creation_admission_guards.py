"""Replace all-parent-lock trigger with immutable creation-time admission.

0017-0021 are retained: 0018 references wallet0010, 0020 references wallet0012.
Existing surcharge parents are marked without repricing any payment/earning.
The marker never changes after this migration. New standalone append is refused.
"""
from django.db import migrations

SQL = r"""
UPDATE orders SET surcharge_guarded=TRUE
WHERE id IN (SELECT order_id FROM orders_ordersurcharge);
CREATE OR REPLACE FUNCTION surcharge_order_guard() RETURNS trigger AS $$
BEGIN
 IF TG_OP='UPDATE' AND NEW.surcharge_guarded IS DISTINCT FROM OLD.surcharge_guarded THEN
  RAISE EXCEPTION 'SURCHARGE_MARKER_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 IF NOT OLD.surcharge_guarded THEN
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='UPDATE' AND EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.id)
   AND (NEW.required_players IS DISTINCT FROM OLD.required_players
   OR NEW.total_amount IS DISTINCT FROM OLD.total_amount
   OR NEW.order_type IS DISTINCT FROM OLD.order_type
   OR NEW.fulfillment_mode IS DISTINCT FROM OLD.fulfillment_mode
   OR NEW.target_player_id IS DISTINCT FROM OLD.target_player_id
   OR NEW.designated_players IS DISTINCT FROM OLD.designated_players) THEN
  RAISE EXCEPTION 'SURCHARGE_SNAPSHOT_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.id
   AND status IN ('processing','unknown')) AND
   (TG_OP='DELETE' OR NEW.status IS DISTINCT FROM OLD.status) THEN
  RAISE EXCEPTION 'SURCHARGE_PAYMENT_PENDING' USING ERRCODE='23514';
 END IF;
 IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.id
   AND status IN ('paid','partially_refunded') AND amount_diamonds>refunded_diamonds)
   AND (TG_OP='DELETE' OR NEW.status IN ('已取消','已完成')) THEN
  RAISE EXCEPTION 'SURCHARGE_LIFECYCLE_UNAVAILABLE' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION surcharge_lineup_guard() RETURNS trigger AS $$
BEGIN
 -- No parent row lock, and no child -> parent inversion. The marker is fixed
 -- before a new order becomes visible; false can never become true later.
 IF TG_OP IN ('INSERT','UPDATE') AND EXISTS (
   SELECT 1 FROM orders WHERE id=NEW.order_id AND surcharge_guarded) THEN
  RAISE EXCEPTION 'SURCHARGE_LIFECYCLE_UNAVAILABLE' USING ERRCODE='23514';
 END IF;
 IF TG_OP IN ('DELETE','UPDATE') AND EXISTS (
   SELECT 1 FROM orders WHERE id=OLD.order_id AND surcharge_guarded) THEN
  RAISE EXCEPTION 'SURCHARGE_LIFECYCLE_UNAVAILABLE' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION surcharge_admission_guard() RETURNS trigger AS $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM orders WHERE id=NEW.order_id AND surcharge_guarded) THEN
  RAISE EXCEPTION 'SURCHARGE_PREORDER_ONLY' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' AND (NEW.order_id IS DISTINCT FROM OLD.order_id
  OR NEW.boss_id IS DISTINCT FROM OLD.boss_id OR NEW.amount_diamonds IS DISTINCT FROM OLD.amount_diamonds
  OR NEW.amount_yuan IS DISTINCT FROM OLD.amount_yuan
  OR NEW.allocation_snapshot IS DISTINCT FROM OLD.allocation_snapshot) THEN
  RAISE EXCEPTION 'SURCHARGE_SNAPSHOT_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER surcharge_admission_guard BEFORE INSERT OR UPDATE ON orders_ordersurcharge
FOR EACH ROW EXECUTE FUNCTION surcharge_admission_guard();
"""


def install(apps, editor):
    if editor.connection.vendor == 'postgresql':
        editor.execute(SQL)
    else:
        Order = apps.get_model('orders', 'Order')
        Surcharge = apps.get_model('orders', 'OrderSurcharge')
        Order.objects.filter(pk__in=Surcharge.objects.values('order_id')).update(surcharge_guarded=True)


def reverse(apps, editor):
    if editor.connection.vendor == 'postgresql':
        editor.execute('DROP TRIGGER surcharge_admission_guard ON orders_ordersurcharge; DROP FUNCTION surcharge_admission_guard(); DROP TRIGGER surcharge_lineup_guard ON order_players; DROP TRIGGER surcharge_order_guard ON orders;')
        from importlib import import_module
        editor.execute(import_module('apps.orders.migrations.0019_surcharge_concurrency_guards').SQL)


class Migration(migrations.Migration):
    dependencies = [('orders', '0021_surcharge_guard_marker')]
    operations = [migrations.RunPython(install, reverse)]
