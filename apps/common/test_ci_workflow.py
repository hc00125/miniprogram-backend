"""CI infrastructure contracts; no database or external transport is mocked."""
import ast
from pathlib import Path
import re
from urllib.parse import urlparse

from django.conf import settings
from django.test import SimpleTestCase

from apps.orders.models import Order


class BackendCIWorkflowTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.workflow = (self.root / '.github/workflows/p0-backend-check.yml').read_text()

    def test_workflow_uses_postgresql_service_for_real_migrations(self):
        match = re.search(r'^      DATABASE_URL: "([^"]+)"$', self.workflow, re.MULTILINE)
        self.assertIsNotNone(match, 'CI must not fall back to SQLite for PostgreSQL DDL')
        database = urlparse(match.group(1))
        self.assertEqual(database.scheme, 'postgresql')
        self.assertEqual(database.hostname, '127.0.0.1')
        self.assertEqual(database.path, '/miniprogram_ci')
        self.assertIn('image: postgres:15-alpine', self.workflow)
        self.assertIn('--health-cmd "pg_isready -U ci -d miniprogram_ci"', self.workflow)

    def test_existing_failure_enforcement_is_not_relaxed(self):
        for check in ['migration_check', 'regression_tests']:
            self.assertIn(f"if: steps.{check}.outputs.exit_code != '0'", self.workflow)
        self.assertIn('cat migration-check.txt\n          exit 1', self.workflow)
        self.assertIn('cat test-results.txt\n          exit 1', self.workflow)
        self.assertNotIn('continue-on-error:', self.workflow)

    def test_payment_test_order_numbers_fit_the_real_schema(self):
        limit = Order._meta.get_field('order_no').max_length
        for filename in ['test_admin_search.py', 'test_closed_virtual_payment.py']:
            tree = ast.parse((self.root / 'apps/payments' / filename).read_text())
            identifiers = [
                keyword.value.value
                for node in ast.walk(tree) if isinstance(node, ast.Call)
                for keyword in node.keywords
                if keyword.arg == 'order_no' and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ]
            self.assertTrue(identifiers, f'Expected an order fixture in {filename}')
            for identifier in identifiers:
                with self.subTest(filename=filename, identifier=identifier):
                    self.assertLessEqual(len(identifier), limit)
