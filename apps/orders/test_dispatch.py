import json
from django.contrib.auth import get_user_model
from django.test import TestCase


class DispatchTests(TestCase):
    def setUp(self):
        self.agent = get_user_model().objects.create_user(username='dispatch-agent', is_superuser=True)
        self.client.force_login(self.agent)

    def post(self, path, data):
        return self.client.post('/dispatch/api/' + path + '/', data=json.dumps(data), content_type='application/json')

    def test_create_customer_assigns_numeric_id_without_creating_login(self):
        count = get_user_model().objects.count()
        response = self.post('customers', {'nickname': '线下老板', 'note': '一群备注'})
        self.assertEqual(response.status_code, 201, response.content[:300])
        self.assertIsInstance(response.json()['id'], int)
        self.assertIsNone(response.json()['user_id'])
        self.assertEqual(get_user_model().objects.count(), count)

    def test_staff_offline_order_uses_catalog_price_and_no_customer_wallet(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order
        from apps.wallet.models import ClientWalletLedger
        from apps.payments.models import Payment
        package = Package.objects.create(name='双人小时', base_price=30, player_count=2)
        customer = self.post('customers', {'nickname': '群老板'}).json()
        data = {'customer_ref': customer['ref'], 'package_id': package.pk, 'hours': 2,
                'game_id': '测试房间', 'boss_note': '普通陪玩'}
        quote = self.post('quote', data)
        self.assertEqual(quote.status_code, 200, quote.content[:300])
        self.assertEqual(quote.json()['total_amount_yuan'], '60.00')
        data.update(quote_version=quote.json()['quote_version'], received=True,
                    receipt_note='已核对微信转账', request_key='00000000-0000-4000-8000-000000000001')
        response = self.post('orders', data)
        self.assertEqual(response.status_code, 201, response.content[:300])
        order = Order.objects.get(order_no=response.json()['order_no'])
        self.assertEqual(order.source, 'staff')
        self.assertEqual(order.customer_id, customer['id'])
        self.assertIsNone(order.boss_user_id)
        self.assertEqual(order.created_by_id, self.agent.pk)
        self.assertEqual(order.status, Order.STATUS_WAITING)
        self.assertTrue(order.paid)
        self.assertEqual(order.total_amount, 60)
        self.assertEqual(order.required_players, 2)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(ClientWalletLedger.objects.count(), 0)

    def make_dispatch(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order
        package = Package.objects.create(name='小时商品', base_price=20, player_count=1)
        customer = self.post('customers', {'nickname': '历史老板', 'note': '内部信息不可公开'}).json()
        data = {'customer_ref': customer['ref'], 'package_id': package.pk, 'hours': 1}
        quoted = self.post('quote', data).json()
        data.update(quote_version=quoted['quote_version'], received=True, receipt_note='已收款',
                    request_key='00000000-0000-4000-8000-000000000002')
        response = self.post('orders', data)
        self.assertEqual(response.status_code, 201, response.content[:300])
        return Order.objects.get(order_no=response.json()['order_no']), customer, data

    def test_claim_requires_staff_review_and_preserves_financial_state(self):
        from apps.accounts.models import ClientProfile, BossConsumptionLedger
        from apps.orders.models import Order
        from apps.earnings.models import PlayerEarning, WalletLedger
        from apps.wallet.models import ClientWalletLedger
        from rest_framework.test import APIClient
        order, customer, _ = self.make_dispatch()
        Order.objects.filter(pk=order.pk).update(status=Order.STATUS_COMPLETED)
        boss = get_user_model().objects.create_user(username='claim-boss')
        ClientProfile.objects.create(user=boss, openid='fixture-claim-boss', nickname='小程序老板')
        api = APIClient(); api.force_authenticate(boss)
        response = api.post('/api/client/history-claims/', {'customer_id': customer['id'], 'message': '群里昵称历史老板'}, format='json')
        self.assertEqual(response.status_code, 201, response.content[:300])
        self.assertNotIn('内部信息', response.content.decode())
        order.refresh_from_db(); self.assertIsNone(order.boss_user_id)
        counts = [model.objects.count() for model in (PlayerEarning, WalletLedger, ClientWalletLedger, BossConsumptionLedger)]
        result = self.post(f"claims/{response.json()['id']}/review", {'decision': 'approve', 'verified': True, 'note': '已在原私聊核实本人'})
        self.assertEqual(result.status_code, 200, result.content[:300])
        order.refresh_from_db()
        self.assertEqual(order.boss_user_id, boss.pk)
        self.assertEqual(order.source, 'staff')
        self.assertEqual(order.payment_method, 'staff_offline')
        self.assertTrue(order.paid)
        self.assertEqual(order.total_amount, 20)
        self.assertEqual(counts, [model.objects.count() for model in (PlayerEarning, WalletLedger, ClientWalletLedger, BossConsumptionLedger)])
        listing = api.get('/api/boss/orders/me')
        self.assertEqual(listing.status_code, 200, listing.content[:300])
        self.assertIn(order.order_no, listing.content.decode())

    def test_console_search_supports_least_privilege_staff_without_admin_flag(self):
        from django.contrib.auth.models import Permission
        from apps.accounts.models import ClientProfile
        staff = get_user_model().objects.create_user(username='limited-staff')
        staff.user_permissions.add(Permission.objects.get(codename='use_console', content_type__app_label='dispatch'))
        self.client.force_login(staff)
        self.post('customers', {'nickname': '同名老板', 'note': '一群'})
        self.post('customers', {'nickname': '同名老板', 'note': '二群'})
        boss = get_user_model().objects.create_user(username='registered')
        ClientProfile.objects.create(user=boss, nickname='已登录老板', openid='fixture-registered')
        page = self.client.get('/dispatch/')
        self.assertEqual(page.status_code, 200, page.content[:300])
        result = self.client.get('/dispatch/api/customers/', {'q': '同名老板'})
        self.assertEqual(result.status_code, 200, result.content[:300])
        self.assertEqual(len(result.json()['results']), 2)
        result = self.client.get('/dispatch/api/customers/', {'q': '已登录老板'})
        self.assertEqual(result.json()['results'][0]['ref'], f'user:{boss.pk}')
        self.assertFalse(staff.is_staff)
        self.assertEqual(self.client.get('/dispatch/api/catalog/').status_code, 200)
        self.assertEqual(self.client.get('/dispatch/api/orders/').status_code, 200)
        self.assertEqual(self.client.get('/dispatch/api/claims/').status_code, 200)

    def test_staff_order_cannot_enter_automatic_payment_or_refund(self):
        from django.db import transaction
        from rest_framework.exceptions import ValidationError
        from apps.wallet.order_spend import guard_order
        from apps.orders.renewals import validate_renewable_order
        order, _, _ = self.make_dispatch()
        with transaction.atomic(), self.assertRaises(ValidationError):
            guard_order(order)
        with transaction.atomic(), self.assertRaises(ValidationError):
            guard_order(order, cancel=True)
        order.status = order.STATUS_READY_TO_START
        with self.assertRaises(ValidationError):
            validate_renewable_order(order)

    def test_source_is_visible_without_internal_customer_note(self):
        from apps.orders.serializers import BossOrderListSerializer, PlayerOrderDetailSerializer
        order, _, _ = self.make_dispatch()
        result = BossOrderListSerializer(order).data
        self.assertEqual(result.get('source'), 'staff')
        self.assertNotIn('内部信息', json.dumps(result, ensure_ascii=False))
        self.assertEqual(PlayerOrderDetailSerializer(order).data['boss_name'], '历史老板')

    def test_full_service_flow_earns_once_and_never_debits_boss(self):
        from apps.catalog.models import PlayerType
        from apps.players.models import Player
        from apps.orders import services
        from apps.earnings.models import PlayerEarning
        from apps.earnings.settlements import create_order_earnings
        from apps.wallet.models import ClientWalletLedger
        order, _, _ = self.make_dispatch()
        kind = PlayerType.objects.create(name='普通陪玩', priority=0, price_extra=0)
        player = Player.objects.create(name='接单员', player_type=kind, status='approved', is_online=True)
        order = services.grab_order(order.order_no, player)
        self.assertEqual(order.status, order.STATUS_READY_TO_START)
        order = services.start_timer(order, player)
        order = services.complete_order(order, player)
        self.assertEqual(order.status, order.STATUS_COMPLETED)
        create_order_earnings(order); create_order_earnings(order)
        self.assertEqual(PlayerEarning.objects.filter(order=order).count(), 1)
        earning = PlayerEarning.objects.get(order=order)
        self.assertEqual(str(earning.gross_amount), '200.00')
        self.assertEqual(str(earning.net_amount), '168.00')
        self.assertFalse(ClientWalletLedger.objects.exists())

    def test_order_idempotency_returns_original_and_mismatched_payload_conflicts(self):
        from apps.orders.models import Order
        order, _, data = self.make_dispatch()
        again = self.post('orders', data)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['order_no'], order.order_no)
        data['boss_note'] = 'changed intent'
        self.assertEqual(self.post('orders', data).status_code, 409)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.client.get('/dispatch/api/submissions/'+data['request_key']+'/').json()['order_no'], order.order_no)

    def test_no_receipt_or_arbitrary_price_is_rejected_without_new_order(self):
        from apps.orders.models import Order
        order, _, data = self.make_dispatch()
        data['request_key'] = '00000000-0000-4000-8000-000000000003'
        data['received'] = False
        self.assertEqual(self.post('orders', data).status_code, 400)
        data['received'] = True; data['total_amount'] = 1
        self.assertEqual(self.post('orders', data).status_code, 400)
        self.assertEqual(Order.objects.count(), 1)

    def test_changed_catalog_price_rejects_old_quote_and_does_not_create_order(self):
        from apps.orders.models import Order
        order, _, data = self.make_dispatch()
        data['request_key'] = '00000000-0000-4000-8000-000000000003'
        order.package.base_price = 99; order.package.save(update_fields=['base_price'])
        self.assertEqual(self.post('orders', data).status_code, 409)
        self.assertEqual(Order.objects.count(), 1)

    def test_public_cannot_search_or_create_and_staff_flag_alone_is_not_permission(self):
        from django.test import Client
        paths = ['customers','catalog','orders','claims']
        guest = Client()
        for p in paths: self.assertEqual(guest.get('/dispatch/api/'+p+'/').status_code, 403)
        user = get_user_model().objects.create_user(username='staff-without-dispatch', is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.post('customers', {'nickname':'forbidden'}).status_code, 403)
        self.assertEqual(self.client.get('/dispatch/').status_code, 403)

    def test_console_post_requires_csrf(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True); client.force_login(self.agent)
        response = client.post('/dispatch/api/customers/', data=json.dumps({'nickname':'csrf'}), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_registered_customer_selection_keeps_boss_separate_from_operator(self):
        from apps.accounts.models import ClientProfile
        from apps.dispatch.models import Customer
        from apps.orders.models import Order
        order, _, data = self.make_dispatch()
        boss = get_user_model().objects.create_user(username='registered-boss')
        ClientProfile.objects.create(user=boss, openid='fixture-real-boss', nickname='已注册')
        data = {k:v for k,v in data.items() if k not in ['quote_version','received','receipt_note','request_key']}
        data['customer_ref'] = f'user:{boss.pk}'
        quoted = self.post('quote', data).json()
        data.update(quote_version=quoted['quote_version'],received=True,receipt_note='线下收款',request_key='00000000-0000-4000-8000-000000000003')
        response = self.post('orders',data); self.assertEqual(response.status_code,201,response.content[:200])
        new = Order.objects.get(order_no=response.json()['order_no'])
        self.assertEqual(new.boss_user_id,boss.pk);self.assertEqual(new.created_by_id,self.agent.pk)
        self.assertEqual(Customer.objects.filter(user=boss).count(),1)

    def test_claim_guessing_does_not_reveal_customers_and_review_without_verification_fails(self):
        from rest_framework.test import APIClient
        from apps.accounts.models import ClientProfile
        from apps.dispatch.models import HistoryClaim
        order, customer, _ = self.make_dispatch()
        boss = get_user_model().objects.create_user(username='claim-probe')
        ClientProfile.objects.create(user=boss, openid='fixture-probe', nickname='申请者')
        api=APIClient(); api.force_authenticate(boss)
        payload={'customer_id':customer['id'],'message':'我来认领'}
        claim=api.post('/api/client/history-claims/',payload,format='json')
        self.assertEqual(claim.status_code,201)
        self.assertNotIn('历史老板',claim.content.decode())
        self.assertEqual(api.post('/api/client/history-claims/',payload,format='json').status_code,200)
        self.assertEqual(HistoryClaim.objects.count(),1)
        denied=self.post(f"claims/{claim.json()['id']}/review",{'decision':'approve','verified':False,'note':'尚未核实'})
        self.assertEqual(denied.status_code,400)
        other=get_user_model().objects.create_user(username='other-applicant')
        ClientProfile.objects.create(user=other,openid='fixture-other',nickname='别人')
        api.force_authenticate(other)
        self.assertEqual(api.get('/api/client/history-claims/').json()['claims'],[])
        self.assertEqual(api.get('/api/boss/order/'+order.order_no).status_code,403)

    def test_login_requires_https_when_production_gate_enabled(self):
        from django.test import Client, override_settings
        with override_settings(DISPATCH_REQUIRE_HTTPS=True):
            response=Client().get('/dispatch/login/')
            self.assertEqual(response.status_code,302)
            self.assertEqual(response['Location'],'https://testserver/dispatch/login/')
            self.assertEqual(Client().get('/dispatch/login/',secure=True).status_code,200)

    def test_console_issues_secure_cookies_without_changing_legacy_site_settings(self):
        from django.test import Client, override_settings
        self.agent.set_password('fixture-no-real-account');self.agent.save(update_fields=['password'])
        with override_settings(DISPATCH_REQUIRE_HTTPS=True,SESSION_COOKIE_SECURE=False,CSRF_COOKIE_SECURE=False):
            client=Client()
            response=client.get('/dispatch/login/',secure=True)
            self.assertTrue(response.cookies['csrftoken']['secure'])
            response=client.post('/dispatch/login/',{'username':self.agent.username,'password':'fixture-no-real-account'},secure=True)
            self.assertEqual(response.status_code,302)
            self.assertTrue(response.cookies['sessionid']['secure'])

    def test_source_column_allows_previous_release_inserts_for_safe_rollback(self):
        from django.db import connection
        from apps.orders.models import Order
        from psycopg2 import sql
        original, _, _ = self.make_dispatch()
        columns=[f.column for f in Order._meta.concrete_fields if f.name not in {'id','order_no','source','customer','created_by'}]
        query=sql.SQL('INSERT INTO {} ({}, {}) SELECT %s, {} FROM {} WHERE id=%s RETURNING id').format(
            sql.Identifier(Order._meta.db_table),sql.Identifier('order_no'),sql.SQL(',').join(map(sql.Identifier,columns)),
            sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(Order._meta.db_table))
        with connection.cursor() as cursor:
            cursor.execute(query,['legacy-schema-probe',original.pk])
            inserted=cursor.fetchone()[0]
        legacy=Order.objects.get(pk=inserted)
        self.assertEqual(legacy.source,'self');self.assertIsNone(legacy.customer_id);self.assertIsNone(legacy.created_by_id)

    def test_login_is_rate_limited(self):
        from django.test import Client
        client=Client()
        for i in range(10):
            response=client.post('/dispatch/login/',{'username':'login-probe','password':'invalid-test-only'})
            self.assertEqual(response.status_code,200)
        response=client.post('/dispatch/login/',{'username':'login-probe','password':'invalid-test-only'})
        self.assertEqual(response.status_code,429)





