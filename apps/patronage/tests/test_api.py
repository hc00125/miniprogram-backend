from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient


class CatalogTests(TestCase):
    def test_default_catalog_is_real_readonly_and_transaction_disabled(self):
        with CaptureQueriesContext(connection) as queries:
            response = APIClient().get('/api/patronage/catalog/')
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['contract_version'], '1.0')
        self.assertFalse(data['purchase_enabled'])
        self.assertIsNone(data['player'])
        self.assertEqual(data['commission_rate'], '0.25')
        self.assertEqual([p['code'] for p in data['packages']],
                         ['day', 'week', 'month', 'quarter', 'year', 'day_pass'])
        self.assertEqual([p['amount_yuan'] for p in data['packages']],
                         ['188.00', '520.00', '1314.00', '2888.00', '9999.00', None])
        self.assertEqual([p['duration_days'] for p in data['packages']], [1, 7, 30, 90, 365, None])
        self.assertEqual(data['packages'][-1]['blockers'], ['HOURLY_RATE_UNSET'])
