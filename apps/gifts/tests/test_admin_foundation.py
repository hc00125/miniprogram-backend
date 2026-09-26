from django.contrib.auth import get_user_model
from django.test import TestCase
from django.contrib import admin
from apps.gifts import models


class FinancialAdminTests(TestCase):
    def test_financial_records_are_read_only_even_for_superuser(self):
        user = get_user_model().objects.create_superuser('auditor', password='test')
        self.client.force_login(user)
        for name in ('giftpurchase', 'giftinventorylot', 'giftinventoryledger', 'gifttransfer',
                     'gifttransferallocation', 'giftearning', 'giftrefund'):
            self.assertEqual(self.client.get('/admin/gifts/%s/' % name).status_code, 200, name)
            self.assertEqual(self.client.get('/admin/gifts/%s/add/' % name).status_code, 403, name)
