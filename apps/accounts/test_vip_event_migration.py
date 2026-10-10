from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class VipEventMigrationTests(TransactionTestCase):
    def test_additive_upgrade_keeps_existing_consumption_and_does_not_backfill(self):
        old = [('accounts', '0007_client_phone_binding')]
        new = [('accounts', '0008_vipupgradeevent')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            state = executor.loader.project_state(old).apps
            Profile = state.get_model('accounts', 'ClientProfile')
            Ledger = state.get_model('accounts', 'BossConsumptionLedger')
            user = get_user_model().objects.create_user(username='legacy-vip-migration')
            profile = Profile.objects.create(user_id=user.pk, openid='legacy-vip-migration', nickname='历史老板', cumulative_consumption=100)
            ledger = Ledger.objects.create(profile=profile, amount=100, balance_after=100, source_type='order', reference_id='legacy')
            executor = MigrationExecutor(connection)
            executor.migrate(new)
            from .models import VipUpgradeEvent, ClientProfile, BossConsumptionLedger
            self.assertEqual(VipUpgradeEvent.objects.count(), 0)
            self.assertEqual(ClientProfile.objects.get(pk=profile.pk).cumulative_consumption, 100)
            self.assertEqual(BossConsumptionLedger.objects.get(pk=ledger.pk).reference_id, 'legacy')
        finally:
            MigrationExecutor(connection).migrate(new)
