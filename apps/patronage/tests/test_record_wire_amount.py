"""Regression: HTTP JSON money must stay string under numeric global DRF settings."""
import importlib.util
import json
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from apps.patronage.serializers import RecordSerializer

if os.environ.get('PATRONAGE_SERIALIZER_CANDIDATE'):
    spec = importlib.util.spec_from_file_location(
        'apps.patronage._wire_candidate', os.environ['PATRONAGE_SERIALIZER_CANDIDATE'])
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    RecordSerializer = candidate.RecordSerializer


def record(amount='520.00', status='paid'):
    now = timezone.now()
    return SimpleNamespace(
        purchase_no='PN-WIRE-TEST', player_id=22,
        player=SimpleNamespace(is_archived=False), player_name='Test player',
        package_code='week', package_name='Week', amount_yuan=Decimal(amount),
        payment_status=status, created_at=now, paid_at=now if status == 'paid' else None,
        starts_at=now if status == 'paid' else None,
        expires_at=now + timedelta(days=7) if status == 'paid' else None,
        bonus_naming_days=0, blockers=[], idempotency_key='wire-test-original-key',
        price_version='a' * 64,
        attempt=SimpleNamespace(status={'paid':'completed', 'created':'prepared'}.get(status,status), blocker=''),
        earning=object(), crowns=SimpleNamespace(exists=lambda: True))


def render(data):
    response = Response(data)
    response.accepted_renderer = JSONRenderer()
    response.accepted_media_type = 'application/json'
    response.renderer_context = {}
    return json.loads(response.render().content)


@override_settings(REST_FRAMEWORK={'COERCE_DECIMAL_TO_STRING': False})
class RecordWireAmountTests(SimpleTestCase):
    def test_purchase_and_by_key_http_money_is_exact_string(self):
        for value in ('0.00', '0.01', '520.00', '9999999999.99'):
            with self.subTest(value=value):
                data = render(RecordSerializer(record(value)).data)
                self.assertEqual(data['amount_yuan'], value)
                self.assertIsInstance(data['amount_yuan'], str)
                self.assertEqual(data['amount_diamonds'], f'{Decimal(value)*10:.1f}')
                self.assertEqual(data['payment_status'], 'paid')
                self.assertEqual(data['idempotency_key'], 'wire-test-original-key')

    def test_records_and_pending_http_money_is_string(self):
        for state in ('created', 'processing', 'unknown', 'paid', 'failed'):
            with self.subTest(state=state):
                data = render({'count':1, 'next':None, 'previous':None,
                    'results':RecordSerializer([record(status=state)], many=True).data})
                self.assertEqual(data['results'][0]['amount_yuan'], '520.00')
                if state != 'paid':
                    self.assertIsNone(data['results'][0]['paid_at'])

    def test_money_fix_preserves_incomplete_fulfillment_guard(self):
        obj = record()
        obj.crowns = SimpleNamespace(exists=lambda: False)
        data = render(RecordSerializer(obj).data)
        self.assertEqual(data['amount_yuan'], '520.00')
        self.assertEqual(data['payment_status'], 'unknown')
        self.assertIn('FULFILLMENT_REVIEW_REQUIRED', data['blockers'])
        self.assertIsNone(data['paid_at'])


if __name__ == '__main__':
    import unittest
    unittest.main()
