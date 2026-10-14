from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from .models import ClientProfile, VipTier
from .vip import record_consumption


class VipAnnouncementTests(TestCase):
    def setUp(self):
        VipTier.objects.all().delete()
        self.low = VipTier.objects.create(code='low', name='初级', min_consumption=0)
        self.high = VipTier.objects.create(code='high', name='高级', min_consumption=100)
        user = get_user_model().objects.create_user(username='vip-event')
        self.profile = ClientProfile.objects.create(user=user, openid='vip-event', nickname='测试老板', vip_tier=self.low)

    def spend(self, amount=100, key='order-1', **kwargs):
        return record_consumption(profile=self.profile, amount=amount, source_type='order', reference_id=key, **kwargs)

    def test_boundary_upgrade_is_persisted_once_with_snapshot(self):
        self.spend()
        model = apps.all_models['accounts'].get('vipupgradeevent')
        self.assertIsNotNone(model, 'Real upgrades need a persistent event, not vip_updated_at')
        event = model.objects.get()
        self.assertEqual((event.from_tier_name, event.to_tier_name), ('初级', '高级'))
        self.assertEqual(event.source.reference_id, 'order-1')
        self.spend()
        self.assertEqual(model.objects.count(), 1)

    def test_backfill_does_not_publish(self):
        from django.core.management import call_command
        from apps.orders.models import Order
        from apps.catalog.models import Package
        from .models import VipUpgradeEvent, BossConsumptionLedger
        order = Order.objects.create(order_no='history', boss_user=self.profile.user,
            package=Package.objects.create(name='隔离套餐', base_price=100, player_count=1),
            required_players=1, total_price_per_hour=100, total_amount=100,
            status=Order.STATUS_IN_PROGRESS, paid=True)
        Order.objects.filter(pk=order.pk).update(status=Order.STATUS_COMPLETED)
        call_command('backfill_boss_consumption', profile_id=self.profile.pk)
        self.assertEqual(BossConsumptionLedger.objects.count(), 1)
        self.assertEqual(VipUpgradeEvent.objects.count(), 0)

    def test_popular_public_snapshot_readonly_and_legacy_kinds(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.spend()
        self.high.name = '改名不改历史'
        self.high.save()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/announcements/', {'kind': 'popular'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(q['sql'].lstrip(' (\n\t').upper().startswith('SELECT') for q in queries), [q['sql'] for q in queries])
        row = response.json()['results'][0]
        self.assertEqual(row['kind'], 'vip_upgrade')
        self.assertEqual(row['to_tier_name'], '高级')
        self.assertEqual(set(row), {'id', 'kind', 'boss_name', 'boss_avatar_url', 'from_tier_name', 'to_tier_name', 'occurred_at', 'text'})
        for kind in ('gift', 'patronage', 'all'):
            self.assertEqual(self.client.get('/api/announcements/', {'kind': kind}).json()['count'], 0)
        self.profile.account_status = 'banned'
        self.profile.save(update_fields=['account_status'])
        self.assertEqual(self.client.get('/api/announcements/', {'kind': 'popular'}).json()['count'], 0)

    def test_consumption_edges_refund_reupgrade_jump_and_snapshots(self):
        from .models import VipUpgradeEvent
        self.spend(99)
        self.assertEqual(VipUpgradeEvent.objects.count(), 0)
        self.spend(1, 'boundary')
        record_consumption(profile=self.profile, amount=-50, source_type='refund', reference_id='refund')
        self.assertEqual(VipUpgradeEvent.objects.count(), 1)
        self.spend(50, 'reupgrade')
        top = VipTier.objects.create(code='top', name='顶级', min_consumption=300)
        VipTier.objects.create(code='middle', name='中级', min_consumption=200)
        self.spend(250, 'jump')
        self.assertEqual(VipUpgradeEvent.objects.count(), 3)
        self.assertEqual(VipUpgradeEvent.objects.first().to_tier_id_snapshot, top.pk)
        self.high.name = '新名'
        self.high.save()
        self.assertEqual(VipUpgradeEvent.objects.order_by('pk').first().to_tier_name, '高级')

    def test_manual_backfill_and_rule_recompute_do_not_publish(self):
        from .models import VipUpgradeEvent
        from .admin import ClientProfileAdmin
        from django.contrib.admin.sites import AdminSite
        from unittest.mock import patch
        for source in ('manual', 'backfill'):
            record_consumption(profile=self.profile, amount=100, source_type=source)
        VipTier.objects.create(code='new', name='运营重算', min_consumption=150)
        admin = ClientProfileAdmin(ClientProfile, AdminSite())
        with patch.object(admin, 'message_user'):
            admin.refresh_vip_display(None, ClientProfile.objects.filter(pk=self.profile.pk))
        self.assertEqual(VipUpgradeEvent.objects.count(), 0)

    def test_rollback_removes_profile_ledger_and_event_together(self):
        from .models import VipUpgradeEvent, BossConsumptionLedger
        from django.db import transaction
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.spend()
                raise RuntimeError('downstream failure')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, 0)
        self.assertEqual(BossConsumptionLedger.objects.count(), 0)
        self.assertEqual(VipUpgradeEvent.objects.count(), 0)
        self.spend()
        self.assertEqual(VipUpgradeEvent.objects.count(), 1)

    def test_stale_profile_from_rule_change_does_not_manufacture_upgrade(self):
        from .models import VipUpgradeEvent
        ClientProfile.objects.filter(pk=self.profile.pk).update(cumulative_consumption=150)
        self.spend(1)
        self.assertEqual(VipUpgradeEvent.objects.count(), 0)

    def test_restrictions_and_inactive_user_are_readonly(self):
        from datetime import timedelta
        from django.utils import timezone
        self.spend()
        for status, until, expected in [('banned', None, 0), ('suspended', None, 0),
                ('suspended', timezone.now()+timedelta(days=1), 0),
                ('suspended', timezone.now()-timedelta(days=1), 1), ('active', None, 1)]:
            ClientProfile.objects.filter(pk=self.profile.pk).update(account_status=status, account_suspended_until=until)
            self.assertEqual(self.client.get('/api/announcements/', {'kind':'popular'}).json()['count'], expected)
            self.profile.refresh_from_db()
            self.assertEqual(self.profile.account_status, status)
        self.profile.user.is_active = False
        self.profile.user.save()
        self.assertEqual(self.client.get('/api/announcements/', {'kind':'popular'}).json()['count'], 0)

    def test_event_cannot_be_updated_or_deleted(self):
        from .models import VipUpgradeEvent
        from django.db import DatabaseError, transaction
        self.spend()
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            VipUpgradeEvent.objects.update(to_tier_name='伪造')
        self.assertEqual(getattr(caught.exception.__cause__, 'sqlstate', None) or getattr(caught.exception.__cause__, 'pgcode', None), '23514')
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            VipUpgradeEvent.objects.all().delete()
        self.assertEqual(getattr(caught.exception.__cause__, 'sqlstate', None) or getattr(caught.exception.__cause__, 'pgcode', None), '23514')

    def test_event_write_failure_rolls_back_existing_consumption(self):
        from .models import VipUpgradeEvent, BossConsumptionLedger
        from unittest.mock import patch
        with patch.object(VipUpgradeEvent.objects, 'create', side_effect=RuntimeError('event persistence failure')):
            with self.assertRaises(RuntimeError):
                self.spend()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, 0)
        self.assertEqual(BossConsumptionLedger.objects.count(), 0)
        self.assertEqual(VipUpgradeEvent.objects.count(), 0)


from django.test import TransactionTestCase


class VipUpgradeConcurrencyTests(TransactionTestCase):
    setUp = VipAnnouncementTests.setUp

    def test_concurrent_same_source_is_one_consumption_and_one_event(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        from .models import BossConsumptionLedger, VipUpgradeEvent
        barrier = Barrier(2)
        def worker():
            try:
                barrier.wait(timeout=10)
                return record_consumption(profile=self.profile, amount=100, source_type='order', reference_id='same').pk
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(ids[0], ids[1])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, 100)
        self.assertEqual(BossConsumptionLedger.objects.count(), 1)
        self.assertEqual(VipUpgradeEvent.objects.count(), 1)

    def test_concurrent_distinct_consumption_crosses_boundary_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        from .models import BossConsumptionLedger, VipUpgradeEvent
        barrier = Barrier(2)
        def worker(key):
            try:
                barrier.wait(timeout=10)
                record_consumption(profile=self.profile, amount=60, source_type='order', reference_id=key)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, ['one', 'two']))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cumulative_consumption, 120)
        self.assertEqual(BossConsumptionLedger.objects.count(), 2)
        self.assertEqual(VipUpgradeEvent.objects.count(), 1)
