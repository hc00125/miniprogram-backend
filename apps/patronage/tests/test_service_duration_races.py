from concurrent.futures import ThreadPoolExecutor
from threading import Event
from decimal import Decimal
from django.test import TransactionTestCase, override_settings
from django.db import transaction, close_old_connections
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.wallet.models import ClientWallet
from apps.patronage.models import PatronageSettings, PlayerNamingServiceConfig
from apps.patronage.quotes import quote_data
from apps.patronage.purchases import prepare


@override_settings(PATRONAGE_PURCHASE_ENABLED=True, WECHAT_VIRTUALPAY_ENV=1)
class DurationRaceTests(TransactionTestCase):
    def test_first_config_creation_serializes_with_purchase_on_player_lock(self):
        owner=get_user_model().objects.create_user('race-owner')
        buyer=get_user_model().objects.create_user('race-buyer')
        ClientProfile.objects.create(user=owner,openid='race-owner',nickname='race-owner')
        profile=ClientProfile.objects.create(user=buyer,openid='race-buyer',nickname='race-buyer')
        ClientWallet.objects.update_or_create(profile=profile,defaults={'balance':20000})
        player=Player.objects.create(user=owner,name='race-player',player_type=PlayerType.objects.create(name='技术pro',priority=4))
        PatronageSettings.objects.create(purchase_enabled=True)
        q=quote_data(buyer,player,'day')
        ready,release,started=Event(),Event(),Event()
        def buy():
            close_old_connections()
            try:
                with transaction.atomic():
                    record,_=prepare(buyer,player_id=player.pk,package_code='day',price_version=q['price_version'],idempotency_key='race')
                    ready.set();self.assertTrue(release.wait(10))
                return record.config_snapshot['quote']['service_hours']
            finally:close_old_connections()
        def edit():
            close_old_connections()
            try:
                c=APIClient();c.force_authenticate(get_user_model().objects.get(pk=owner.pk));started.set()
                return c.post('/api/patronage/my-service-durations/',{'package_code':'day','service_hours':'33.5'},format='json').status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(buy);self.assertTrue(ready.wait(10));b=pool.submit(edit);self.assertTrue(started.wait(10))
            self.assertFalse(b.done());release.set()
            self.assertEqual(a.result(15),'2.5');self.assertEqual(b.result(15),200)
        self.assertEqual(PlayerNamingServiceConfig.objects.get().service_hours,Decimal('33.5'))
        self.assertNotEqual(quote_data(buyer,player,'day')['price_version'],q['price_version'])
