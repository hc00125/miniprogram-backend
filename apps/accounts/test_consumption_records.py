from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from .models import BossConsumptionLedger, ClientProfile


class ConsumptionRecordApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='growth-record-user')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='growth-record-openid',
            nickname='成长记录测试用户',
            nickname_customized=True,
            cumulative_consumption=Decimal('30.00'),
        )
        BossConsumptionLedger.objects.create(
            profile=self.profile,
            amount=Decimal('50.00'),
            balance_after=Decimal('50.00'),
            source_type=BossConsumptionLedger.TYPE_ORDER,
            reference_id='order-test-1',
            reason='订单完成计入累计消费',
        )
        BossConsumptionLedger.objects.create(
            profile=self.profile,
            amount=Decimal('-20.00'),
            balance_after=Decimal('30.00'),
            source_type=BossConsumptionLedger.TYPE_REFUND,
            reference_id='refund-test-1',
            reason='退款成功扣减累计消费',
        )
        self.client.force_authenticate(user=self.user)

    def test_list_growth_records_returns_current_growth_and_ledger(self):
        response = self.client.get('/api/client/consumption-records', {'page': 1, 'page_size': 20})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(Decimal(str(response.data['growth_diamonds'])), Decimal('300.0'))
        self.assertEqual(response.data['results'][0]['source_type'], BossConsumptionLedger.TYPE_REFUND)
        self.assertEqual(Decimal(str(response.data['results'][0]['amount_diamonds'])), Decimal('-200.0'))
        self.assertEqual(Decimal(str(response.data['results'][0]['balance_after_diamonds'])), Decimal('300.0'))
        self.assertEqual(response.data['results'][1]['source_type'], BossConsumptionLedger.TYPE_ORDER)
        self.assertEqual(Decimal(str(response.data['results'][1]['amount_diamonds'])), Decimal('500.0'))

    def test_growth_records_require_login(self):
        self.client.force_authenticate(user=None)

        response = self.client.get('/api/client/consumption-records')

        self.assertIn(response.status_code, {401, 403})
