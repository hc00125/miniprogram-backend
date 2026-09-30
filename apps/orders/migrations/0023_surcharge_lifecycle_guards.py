"""Enable audited service lineup/completion, retain raw SQL fail-closed guards.

No parent locks in child triggers. Ordinary unmarked orders take the fast path.
Existing paid rows are NOT backfilled with an invented settlement snapshot.
"""
from django.db import migrations
from importlib import import_module

SQL = r"""
CREATE OR REPLACE FUNCTION surcharge_lifecycle_valid(oid bigint) RETURNS boolean AS $$
 SELECT EXISTS (
   SELECT 1 FROM orders_ordersurcharge s JOIN wallet_walletspendattempt a ON a.id=s.attempt_id
   JOIN orders o ON o.id=s.order_id
   WHERE s.order_id=oid AND s.status='paid' AND s.refunded_diamonds=0
     AND a.status='completed' AND o.paid
     AND s.policy_snapshot->>'lifecycle'='original-order-v1'
     AND (s.allocation_snapshot->>'required_players')::integer=o.required_players
 ) AND NOT EXISTS (
   SELECT 1 FROM orders_ordersurcharge s WHERE s.order_id=oid
     AND s.status NOT IN ('paid','failed','cancelled','refunded')
 );
$$ LANGUAGE sql STABLE;
CREATE OR REPLACE FUNCTION surcharge_lineup_guard() RETURNS trigger AS $$
DECLARE oid bigint; guarded boolean; parent_status text; required integer;
BEGIN
 IF TG_OP='UPDATE' AND NEW.order_id IS DISTINCT FROM OLD.order_id AND
   EXISTS(SELECT 1 FROM orders WHERE id IN (OLD.order_id,NEW.order_id) AND surcharge_guarded) THEN
   RAISE EXCEPTION 'SURCHARGE_SNAPSHOT_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN oid=OLD.order_id; ELSE oid=NEW.order_id; END IF;
 SELECT surcharge_guarded,status,required_players INTO guarded,parent_status,required FROM orders WHERE id=oid;
 IF guarded THEN
   IF current_setting('touchi.surcharge_lineup',true) IS DISTINCT FROM oid::text
     OR NOT surcharge_lifecycle_valid(oid) OR parent_status IN ('已完成','已取消') THEN
     RAISE EXCEPTION 'SURCHARGE_LIFECYCLE_UNAVAILABLE' USING ERRCODE='23514';
   END IF;
   IF TG_OP='INSERT' AND (SELECT count(*) FROM order_players WHERE order_id=oid)>=required THEN
     RAISE EXCEPTION 'SURCHARGE_LINEUP_FULL' USING ERRCODE='23514';
   END IF;
   IF TG_OP='UPDATE' AND NEW.player_id IS DISTINCT FROM OLD.player_id THEN
     RAISE EXCEPTION 'SURCHARGE_MEMBER_IMMUTABLE' USING ERRCODE='23514';
   END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
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
 IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.id AND status IN ('processing','unknown'))
   AND (TG_OP='DELETE' OR NEW.status IS DISTINCT FROM OLD.status) THEN
  RAISE EXCEPTION 'SURCHARGE_PAYMENT_PENDING' USING ERRCODE='23514';
 END IF;
 IF EXISTS (SELECT 1 FROM orders_ordersurcharge WHERE order_id=OLD.id
   AND status IN ('paid','partially_refunded') AND amount_diamonds>refunded_diamonds) THEN
   IF TG_OP='DELETE' OR NEW.status='已取消' THEN
     RAISE EXCEPTION 'SURCHARGE_LIFECYCLE_UNAVAILABLE' USING ERRCODE='23514';
   END IF;
   IF NEW.status='已完成' AND OLD.status IS DISTINCT FROM NEW.status AND (
     current_setting('touchi.surcharge_lineup',true) IS DISTINCT FROM OLD.id::text
     OR NOT surcharge_lifecycle_valid(OLD.id)
     OR (SELECT count(*) FROM order_players WHERE order_id=OLD.id)<>OLD.required_players
     OR EXISTS(SELECT 1 FROM order_players WHERE order_id=OLD.id AND status<>'已完成')) THEN
     RAISE EXCEPTION 'SURCHARGE_LIFECYCLE_UNAVAILABLE' USING ERRCODE='23514';
   END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION surcharge_policy_guard() RETURNS trigger AS $$
BEGIN
 IF NEW.policy_snapshot IS DISTINCT FROM OLD.policy_snapshot THEN
   RAISE EXCEPTION 'SURCHARGE_POLICY_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER surcharge_policy_guard BEFORE UPDATE ON orders_ordersurcharge
FOR EACH ROW EXECUTE FUNCTION surcharge_policy_guard();
"""


def install(apps, editor):
    if editor.connection.vendor == 'postgresql':
        editor.execute(SQL)


def reverse(apps, editor):
    if editor.connection.vendor == 'postgresql':
        editor.execute('DROP TRIGGER surcharge_policy_guard ON orders_ordersurcharge; DROP FUNCTION surcharge_policy_guard();')
        old = import_module('apps.orders.migrations.0022_surcharge_creation_admission_guards').SQL
        editor.execute(old[:old.index('CREATE OR REPLACE FUNCTION surcharge_admission_guard')])
        editor.execute('DROP FUNCTION surcharge_lifecycle_valid(bigint);')


class Migration(migrations.Migration):
    dependencies = [('orders', '0022_surcharge_creation_admission_guards'), ('earnings', '0007_surcharge_income_source')]
    operations = [migrations.RunPython(install, reverse)]
