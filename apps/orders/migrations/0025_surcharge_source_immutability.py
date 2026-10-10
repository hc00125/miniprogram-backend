"""Protect new-source principal and committed refund request bytes."""
from django.db import migrations

SQL = r"""
CREATE FUNCTION checkout_refund_guard() RETURNS trigger AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'CHECKOUT_REFUND_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 IF NEW.checkout_id IS DISTINCT FROM OLD.checkout_id
    OR NEW.base_refund_id IS DISTINCT FROM OLD.base_refund_id
    OR NEW.refund_no IS DISTINCT FROM OLD.refund_no
    OR NEW.surcharge_amount IS DISTINCT FROM OLD.surcharge_amount
    OR NEW.source_snapshot IS DISTINCT FROM OLD.source_snapshot
    OR NEW.request_payload IS DISTINCT FROM OLD.request_payload THEN
   RAISE EXCEPTION 'CHECKOUT_REFUND_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 IF NEW.remote_status IS DISTINCT FROM OLD.remote_status AND NOT (
   (OLD.remote_status='prepared' AND NEW.remote_status='dispatching') OR
   (OLD.remote_status='dispatching' AND NEW.remote_status IN ('succeeded','unknown')) OR
   (OLD.remote_status='unknown' AND NEW.remote_status='succeeded')) THEN
   RAISE EXCEPTION 'CHECKOUT_REFUND_TRANSITION_INVALID' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER checkout_refund_guard BEFORE UPDATE OR DELETE ON orders_ordercheckoutrefund
FOR EACH ROW EXECUTE FUNCTION checkout_refund_guard();
CREATE FUNCTION surcharge_earning_guard() RETURNS trigger AS $$
BEGIN
 IF TG_OP='UPDATE' AND NEW.source IS DISTINCT FROM OLD.source THEN
   RAISE EXCEPTION 'EARNING_SOURCE_IMMUTABLE' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN
   IF OLD.source='surcharge' THEN
     RAISE EXCEPTION 'SURCHARGE_EARNING_IMMUTABLE' USING ERRCODE='23514';
   END IF;
   RETURN OLD;
 END IF;
 IF NEW.source='surcharge' THEN
   IF TG_OP='UPDATE' AND (NEW.surcharge_id IS DISTINCT FROM OLD.surcharge_id
       OR NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.player_id IS DISTINCT FROM OLD.player_id
       OR NEW.gross_amount IS DISTINCT FROM OLD.gross_amount
       OR NEW.commission_rate IS DISTINCT FROM OLD.commission_rate
       OR NEW.commission_amount IS DISTINCT FROM OLD.commission_amount
       OR NEW.net_amount IS DISTINCT FROM OLD.net_amount
       OR NEW.source_snapshot IS DISTINCT FROM OLD.source_snapshot
       OR NEW.review_until IS DISTINCT FROM OLD.review_until) THEN
     RAISE EXCEPTION 'SURCHARGE_EARNING_IMMUTABLE' USING ERRCODE='23514';
   END IF;
   IF NOT EXISTS(SELECT 1 FROM orders_ordersurcharge s
       WHERE s.id=NEW.surcharge_id AND s.order_id=NEW.order_id
       AND (s.policy_snapshot->>'per_person_gross_fish')::numeric=NEW.gross_amount
       AND (s.policy_snapshot->>'per_person_commission_fish')::numeric=NEW.commission_amount
       AND (s.policy_snapshot->>'per_person_net_fish')::numeric=NEW.net_amount)
       OR NEW.commission_rate<>25 OR NEW.gross_amount<>NEW.commission_amount+NEW.net_amount THEN
     RAISE EXCEPTION 'SURCHARGE_EARNING_SOURCE_INVALID' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER surcharge_earning_guard BEFORE INSERT OR UPDATE OR DELETE ON player_earnings
FOR EACH ROW EXECUTE FUNCTION surcharge_earning_guard();
"""


def install(apps, editor):
    if editor.connection.vendor == 'postgresql':
        editor.execute(SQL)


def reverse(apps, editor):
    if editor.connection.vendor == 'postgresql':
        editor.execute('DROP TRIGGER checkout_refund_guard ON orders_ordercheckoutrefund; DROP FUNCTION checkout_refund_guard(); DROP TRIGGER surcharge_earning_guard ON player_earnings; DROP FUNCTION surcharge_earning_guard();')


class Migration(migrations.Migration):
    dependencies = [('orders', '0024_checkout_refund')]
    operations = [migrations.RunPython(install, reverse)]
