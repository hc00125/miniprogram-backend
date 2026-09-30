from django.db import migrations


SQL = """
CREATE OR REPLACE FUNCTION surcharge_order_guard() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.id
             AND status IN ('processing','unknown')) THEN
    IF TG_OP = 'DELETE' OR NEW.status IS DISTINCT FROM OLD.status
       OR NEW.fulfillment_mode IS DISTINCT FROM OLD.fulfillment_mode
       OR NEW.target_player_id IS DISTINCT FROM OLD.target_player_id
       OR NEW.designated_players IS DISTINCT FROM OLD.designated_players
       OR NEW.order_type IS DISTINCT FROM OLD.order_type THEN
      RAISE EXCEPTION 'SURCHARGE_PAYMENT_PENDING' USING ERRCODE='23514';
    END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.id
             AND status IN ('paid','partially_refunded') AND amount_diamonds>refunded_diamonds)
     AND (TG_OP='DELETE' OR NEW.status='已取消') THEN
    RAISE EXCEPTION 'SURCHARGE_REFUND_REQUIRES_REVIEW' USING ERRCODE='23514';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER surcharge_order_guard BEFORE UPDATE OR DELETE ON orders
FOR EACH ROW EXECUTE FUNCTION surcharge_order_guard();
CREATE OR REPLACE FUNCTION surcharge_lineup_guard() RETURNS trigger AS $$
DECLARE oid bigint;
BEGIN
  IF TG_OP='DELETE' THEN oid=OLD.order_id; ELSE oid=NEW.order_id; END IF;
  -- All lineup writers, including bulk/admin/alternate accept paths, serialize
  -- with surcharge prepare's parent order lock.
  IF TG_OP='UPDATE' THEN
    -- Parent order IDs must be locked in ascending order on moves as well.
    PERFORM id FROM orders WHERE id IN (OLD.order_id, NEW.order_id) ORDER BY id FOR UPDATE;
  ELSE
    PERFORM id FROM orders WHERE id=oid FOR UPDATE;
  END IF;
  IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=oid
             AND status IN ('processing','unknown')) THEN
    RAISE EXCEPTION 'SURCHARGE_PAYMENT_PENDING' USING ERRCODE='23514';
  END IF;
  IF TG_OP IN ('INSERT','UPDATE') AND EXISTS (
      SELECT 1 FROM orders_ordersurcharge WHERE order_id=oid
      AND status IN ('paid','partially_refunded') AND amount_diamonds>refunded_diamonds) THEN
    RAISE EXCEPTION 'SURCHARGE_ALLOCATION_UNCONFIRMED' USING ERRCODE='23514';
  END IF;
  IF TG_OP='UPDATE' AND OLD.order_id IS DISTINCT FROM NEW.order_id THEN
    PERFORM id FROM orders WHERE id=OLD.order_id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.order_id
               AND status IN ('processing','unknown','paid','partially_refunded')) THEN
      RAISE EXCEPTION 'SURCHARGE_LINEUP_REQUIRES_REVIEW' USING ERRCODE='23514';
    END IF;
  END IF;
  IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=oid
      AND status IN ('paid','partially_refunded') AND amount_diamonds>refunded_diamonds) THEN
    RAISE EXCEPTION 'SURCHARGE_LINEUP_REQUIRES_REVIEW' USING ERRCODE='23514';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER surcharge_lineup_guard BEFORE INSERT OR UPDATE OR DELETE ON order_players
FOR EACH ROW EXECUTE FUNCTION surcharge_lineup_guard();
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(SQL)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER surcharge_lineup_guard ON order_players; DROP FUNCTION surcharge_lineup_guard(); DROP TRIGGER surcharge_order_guard ON orders; DROP FUNCTION surcharge_order_guard();')


class Migration(migrations.Migration):
    dependencies = [('orders', '0018_ordersurcharge_attempt_and_more')]
    operations = [migrations.RunPython(install, uninstall)]
