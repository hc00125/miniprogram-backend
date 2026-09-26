from django.contrib import admin
from django.test import TestCase

from apps.payments.models import Payment, Refund

from .models import RefundPayment, RefundRecord


class RefundAdminSeparationTests(TestCase):
    def test_refund_models_are_registered_under_standalone_admin_app(self):
        self.assertTrue(admin.site.is_registered(Payment))
        self.assertFalse(admin.site.is_registered(Refund))
        self.assertTrue(admin.site.is_registered(RefundPayment))
        self.assertTrue(admin.site.is_registered(RefundRecord))
        self.assertEqual(RefundPayment._meta.app_config.verbose_name, '退款管理')
        self.assertEqual(RefundRecord._meta.app_config.verbose_name, '退款管理')

    def test_payment_admin_no_longer_exposes_refund_actions(self):
        payment_admin = admin.site._registry[Payment]
        self.assertEqual(payment_admin.actions, [])
        self.assertNotIn('wechat_original_refund_state', payment_admin.list_display)

    def test_refund_workbench_exposes_ordered_refund_actions(self):
        refund_admin = admin.site._registry[RefundPayment]
        self.assertEqual(
            refund_admin.actions,
            [
                'refund_to_diamonds',
                'submit_wechat_original_refund',
                'sync_wechat_original_refund_status',
            ],
        )
