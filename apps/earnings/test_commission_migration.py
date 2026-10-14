from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class CommissionMigrationTests(TransactionTestCase):
    def test_existing_order_keeps_legacy_policy_and_no_roster_snapshot(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([('orders','0027_order_source_database_default')])
            state=executor.loader.project_state([('orders','0027_order_source_database_default')]).apps
            Package=state.get_model('catalog','Package');Order=state.get_model('orders','Order')
            p=Package.objects.create(name='historical',player_count=1,base_price=100)
            old=Order.objects.create(order_no='migration-history',boss_wechat='offline',package=p,required_players=1,total_amount=100,paid=True)
            executor=MigrationExecutor(connection);executor.migrate(latest)
            from apps.orders.models import Order as CurrentOrder
            restored=CurrentOrder.objects.get(pk=old.pk)
            self.assertEqual(restored.commission_policy,'');self.assertEqual(restored.total_amount,100)
            fresh=CurrentOrder.objects.create(order_no='migration-new',boss_wechat='offline',package_id=p.pk,required_players=1,total_amount=100)
            self.assertEqual(fresh.commission_policy,'standard25-mini16-v1')
            with connection.cursor() as cursor:
                cursor.execute("SELECT column_default FROM information_schema.columns WHERE table_name='orders' AND column_name='commission_policy'")
                self.assertIn("''",cursor.fetchone()[0])
        finally:
            MigrationExecutor(connection).migrate(latest)
