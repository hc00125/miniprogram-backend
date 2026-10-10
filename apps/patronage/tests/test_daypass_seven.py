from decimal import Decimal
from django.test import TransactionTestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.models import PlayerPatronageConfig, PatronageSettings, PatronagePurchase
from apps.wallet.models import ClientWallet
from apps.wallet.diamonds import format_diamonds

@override_settings(PATRONAGE_PURCHASE_ENABLED=True, WECHAT_VIRTUALPAY_ENV=1)
class SevenHourTests(TransactionTestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('seven-hour-buyer')
        self.profile=ClientProfile.objects.create(user=self.user,openid='seven-hour-isolated')
        ClientWallet.objects.update_or_create(profile=self.profile,defaults={'balance':Decimal('10000')})
        PatronageSettings.objects.create(purchase_enabled=True,commission_rate=Decimal('.25'))
        self.client=APIClient();self.client.force_authenticate(self.user)
    def test_admin_help_uses_seven_hour_pricing_not_old_discount(self):
        from django.contrib import admin
        from django.test import RequestFactory
        request=RequestFactory().get('/')
        request.user=get_user_model().objects.create_superuser('daypass-form-admin','a@example.test')
        form=admin.site._registry[PlayerPatronageConfig].get_form(request)
        help_text=form.base_fields['hourly_rate_yuan'].help_text
        self.assertIn('7小时',help_text)
        self.assertNotIn('0.85',help_text)
    def player(self,name,rate=None):
        p=Player.objects.create(name=name,status='approved',is_publicly_visible=True,player_type=PlayerType.objects.create(name=name,priority=1))
        if rate is not None:PlayerPatronageConfig.objects.create(player=p,hourly_rate_yuan=Decimal(rate),enabled=True)
        return p
    def quote(self,p):
        return self.client.post('/api/patronage/quotes/',{'player_id':p.pk,'package_code':'day_pass'},format='json')
    def test_six_approved_totals_exact_shared_units_and_25_percent(self):
        for name,hourly,total in [('娱乐mini','30','210'),('技术mini','40','280'),('金牌陪','50','350'),('明星陪','55','385'),('女陪','50','350'),('技术女陪','60','420')]:
            with self.subTest(level=name):
                p=self.player(name,hourly);response=self.quote(p)
                self.assertEqual(response.status_code,200,response.data)
                q=response.json();self.assertEqual(Decimal(q['amount_yuan']),Decimal(total))
                self.assertEqual(q['amount_diamonds'],format_diamonds(total))
                self.assertEqual(Decimal(q['commission_rate']),Decimal('.25'))
                self.assertEqual(Decimal(q['platform_amount_yuan']),Decimal(total)*Decimal('.25'))
                self.assertTrue(q['can_submit'])
                # Pricing factor is not a newly invented service time limit.
                self.assertIsNone(q['service_hours'])
    def test_pro_unconfigured_and_ineligible_remain_blocked(self):
        for name in ['娱乐pro','技术pro']:
            p=self.player(name);self.assertEqual(self.quote(p).json()['code'],'HOURLY_RATE_UNSET')
        p=self.player('娱乐mini','30');p.is_publicly_visible=False;p.save(update_fields=['is_publicly_visible'])
        self.assertNotEqual(self.quote(p).status_code,200)
    def test_purchase_snapshot_and_bonus_unchanged_after_rate_change(self):
        p=self.player('技术mini','40');q=self.quote(p).json()
        payload=dict(player_id=p.pk,package_code='day_pass',price_version=q['price_version'],idempotency_key='seven-offline-key')
        response=self.client.post('/api/patronage/purchases/',payload,format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.json()['payment_status'],'paid')
        purchase=PatronagePurchase.objects.get();before={k:getattr(purchase,k) for k in purchase.IMMUTABLE}
        self.assertEqual(purchase.amount_yuan,Decimal('280'))
        self.assertEqual(purchase.bonus_naming_days,7)
        self.assertEqual(purchase.crowns.count(),1)
        cfg=PlayerPatronageConfig.objects.get(player=p);cfg.hourly_rate_yuan=Decimal('60');cfg.save()
        self.assertEqual(Decimal(self.quote(p).json()['amount_yuan']),Decimal('420'))
        purchase.refresh_from_db();self.assertEqual(before,{k:getattr(purchase,k) for k in purchase.IMMUTABLE})
        read=self.client.get('/api/patronage/purchases/by-key/',{'idempotency_key':'seven-offline-key'})
        self.assertEqual(Decimal(read.json()['amount_yuan']),Decimal('280'))
