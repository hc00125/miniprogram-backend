from decimal import Decimal
from django.test import TransactionTestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.pricing import catalog_data


@override_settings(PATRONAGE_PURCHASE_ENABLED=True, WECHAT_VIRTUALPAY_ENV=1)
class ServiceDurationTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('duration-player')
        ClientProfile.objects.create(user=self.user, openid='duration-player')
        self.player = Player.objects.create(user=self.user, name='duration', player_type=PlayerType.objects.create(name='娱乐mini', priority=2))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_owner_half_hour_edit_and_purchase_snapshot(self):
        from apps.wallet.models import ClientWallet
        from apps.patronage.models import PatronageSettings
        PatronageSettings.objects.create(purchase_enabled=True)
        ClientWallet.objects.update_or_create(profile=self.user.client_profile, defaults={'balance': Decimal('20000')})
        url = '/api/patronage/my-service-durations/'
        response = self.client.post(url, {'package_code': 'day', 'service_hours': '26.5'}, format='json')
        self.assertEqual(response.status_code, 200)
        q = self.client.post('/api/patronage/quotes/', {'player_id': self.player.pk, 'package_code': 'day'}, format='json').json()
        self.assertEqual(q['service_hours'], '26.5')
        self.assertEqual(q['amount_yuan'], '188.00')
        payload = dict(player_id=self.player.pk, package_code='day', price_version=q['price_version'], idempotency_key='duration-key')
        bought = self.client.post('/api/patronage/purchases/', payload, format='json')
        self.assertEqual(bought.status_code, 200, bought.data)
        self.assertEqual(bought.json()['service_hours'], '26.5')
        self.client.post(url, {'package_code': 'day', 'service_hours': '30'}, format='json')
        old = self.client.get('/api/patronage/purchases/by-key/', {'idempotency_key':'duration-key'}).json()
        self.assertEqual(old['service_hours'], '26.5')
        self.assertEqual(self.client.post('/api/patronage/purchases/', payload, format='json').json()['purchase_no'], old['purchase_no'])
        new = self.client.post('/api/patronage/quotes/', {'player_id': self.player.pk, 'package_code': 'day'}, format='json').json()
        self.assertNotEqual(new['price_version'], q['price_version'])

    def test_permissions_validation_noop_and_other_package_versions(self):
        from apps.patronage.models import PlayerNamingServiceConfig
        from apps.patronage.quotes import quote_data
        url='/api/patronage/my-service-durations/'
        original=quote_data(self.user,self.player,'week')['price_version']
        for bad in ['0','-1','2.25','NaN','Infinity',True,[],{},None]:
            self.assertEqual(self.client.post(url,{'package_code':'day','service_hours':bad},format='json').status_code,400, str(bad))
        for patch in [{'player_id':self.player.pk},{'amount_yuan':'1'}]:
            self.assertEqual(self.client.post(url,dict(package_code='day',service_hours='3',**patch),format='json').status_code,400)
        self.assertFalse(PlayerNamingServiceConfig.objects.exists())
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertFalse(PlayerNamingServiceConfig.objects.exists())
        self.client.post(url,{'package_code':'day','service_hours':'3'},format='json')
        row=PlayerNamingServiceConfig.objects.get(); stamp=row.updated_at
        self.client.post(url,{'package_code':'day','service_hours':'3.0'},format='json')
        row.refresh_from_db();self.assertEqual(row.updated_at,stamp)
        self.assertEqual(quote_data(self.user,self.player,'week')['price_version'],original)
        self.client.force_authenticate(None);self.assertIn(self.client.post(url,{},format='json').status_code,[401,403])
        stranger=get_user_model().objects.create_user('stranger')
        self.client.force_authenticate(stranger);self.assertEqual(self.client.post(url,{},format='json').status_code,403)
        self.client.force_authenticate(self.user)
        from apps.common.player_model_version import archive_filter
        archive_updates = [{'is_archived': True}] if archive_filter(Player) else []
        for update in [{'status':'disabled'}, *archive_updates, {'status':'pending'}]:
            Player.objects.filter(pk=self.player.pk).update(status='approved', **archive_filter(Player))
            Player.objects.filter(pk=self.player.pk).update(**update)
            self.user= get_user_model().objects.get(pk=self.user.pk);self.client.force_authenticate(self.user)
            self.assertEqual(self.client.post(url,{'package_code':'day','service_hours':'4'},format='json').status_code,403)

    def test_unknown_level_is_not_guessed_and_explicit_hours_work(self):
        self.player.player_type.name='女陪';self.player.player_type.save()
        self.assertIsNone(catalog_data(self.player)['packages'][0]['service_hours'])
        self.client.post('/api/patronage/my-service-durations/',{'package_code':'year','service_hours':'999.5'},format='json')
        self.assertEqual(catalog_data(self.player)['packages'][4]['service_hours'],'999.5')

    def test_stale_quote_dispatch_and_old_snapshot_are_safe(self):
        from apps.wallet.models import ClientWallet
        from apps.patronage.models import PatronageSettings, PatronagePurchase
        from apps.patronage.purchases import prepare
        from apps.patronage.quotes import quote_data
        from apps.wallet import spend_service
        from apps.patronage.serializers import RecordSerializer
        PatronageSettings.objects.create(purchase_enabled=True)
        ClientWallet.objects.update_or_create(profile=self.user.client_profile,defaults={'balance':20000})
        q=quote_data(self.user,self.player,'day')
        record,_=prepare(self.user,player_id=self.player.pk,package_code='day',price_version=q['price_version'],idempotency_key='prepared')
        self.client.post('/api/patronage/my-service-durations/',{'package_code':'day','service_hours':'3.5'},format='json')
        with self.assertRaises(Exception) as error:
            prepare(self.user,player_id=self.player.pk,package_code='day',price_version=q['price_version'],idempotency_key='stale')
        self.assertIn('PRICE_CHANGED',str(error.exception))
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            spend_service.execute(record.attempt_id)
        record.refresh_from_db();record.attempt.refresh_from_db()
        self.assertEqual(record.attempt.status,'failed')
        self.assertEqual(RecordSerializer(record).data['service_hours'],'5')
        old=PatronagePurchase.objects.create(boss=self.user,player=self.player,player_name='old',package_code='day',package_name='日冠',amount_yuan=188,commission_rate=Decimal('.25'),platform_amount_yuan=47,player_amount_yuan=141,price_version='a'*64,config_snapshot={},idempotency_key='historical')
        self.assertIsNone(RecordSerializer(old).data['service_hours'])

    def test_complete_default_matrix_without_writing_or_price_changes(self):
        names = ['娱乐mini', '技术mini', '娱乐pro', '技术pro', '金牌陪', '明星陪']
        rows = [[5,4,3,2.5,2,1.5],[14,10,8,6,5,4],[33,25,20,16,13,10],[78,58,48,36,29,23],[268,208,168,128,105,82]]
        for i, name in enumerate(names):
            self.player.player_type.name = name
            packages = catalog_data(self.player)['packages'][:5]
            for j, p in enumerate(packages):
                self.assertEqual(Decimal(p.get('service_hours') or '0'), Decimal(str(rows[j][i])))
            self.assertEqual([p['amount_yuan'] for p in packages], ['188.00','520.00','1314.00','2888.00','9999.00'])
