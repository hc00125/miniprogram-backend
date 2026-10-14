from decimal import Decimal
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.gifts.tests import test_purchases as fixtures
from apps.gifts.services import purchases
from apps.gifts.models import Gift, GiftPurchase
from apps.wallet.models import WalletSpendAttempt, RechargeOrder, ClientWalletLedger
from apps.wallet import spend_service

POLICY={'version':'safety','platform_approved':True,'max_quantity':10,'max_diamonds':1000,'daily_diamonds':2000,'recipient_ids':[]}
@override_settings(WECHAT_VIRTUALPAY_ENV=1,GIFT_PURCHASE_ENABLED=True,GIFT_TRANSACTION_POLICY=POLICY)
class PreparedSafetyTests(TransactionTestCase):
    setUp=fixtures.PurchaseFlowTests.setUp
    def intent(self):
        data=dict(gift_code=self.gift.code, quantity=1, mode='inventory')
        q=purchases.quote(self.user,**data)
        return dict(data,price_version=q['price_version'],idempotency_key='safety')
    def prepare(self):
        data=self.intent()
        with patch('apps.wallet.spend_service.execute',side_effect=RuntimeError('before dispatch')):
            with self.assertRaises(RuntimeError): purchases.purchase(self.user,**data)
        return data, WalletSpendAttempt.objects.get()
    def test_auth_rejection_releases_provably_undispatched(self):
        RechargeOrder.objects.create(recharge_no='credit',profile=self.profile,amount=Decimal('5'),status='credited',notify_payload={'mode':'short_series_coin','wechat_coin_units_per_yuan':10})
        ClientWalletLedger.objects.create(wallet=self.wallet,entry_type='recharge',amount=Decimal('5'),balance_after=Decimal('5'),reference_id='credit')
        class BadAuth:
            def authenticate(self,*a): raise ValidationError('bad code')
            def spend(self,*a): raise AssertionError('must never dispatch')
        with self.assertRaises(ValidationError): purchases.purchase(self.user,**self.intent(),adapter=BadAuth())
        attempt=WalletSpendAttempt.objects.get()
        self.assertEqual(attempt.status,'failed')
        self.assertEqual(attempt.reserved_amount,0)
        self.assertEqual(GiftPurchase.objects.get().status,'failed')
        spend_service.reserve(self.profile,kind='gift',business_no='second',key='second',amount=Decimal('1.00'),intent={})
    def test_disabled_prepared_first_dispatch_rejected_and_released(self):
        data, attempt=self.prepare()
        with override_settings(GIFT_PURCHASE_ENABLED=False, GIFT_TRANSACTION_POLICY={}):
            with self.assertRaises(ValidationError): purchases.purchase(self.user,**data)
        attempt.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(attempt.status,'failed')
        self.assertEqual(attempt.reserved_amount,0)
        self.assertEqual(self.wallet.balance,Decimal('10'))

    def test_policy_change_without_version_bump_rejects_prepared(self):
        data,attempt=self.prepare()
        changed=dict(POLICY,daily_diamonds=1)
        with override_settings(GIFT_TRANSACTION_POLICY=changed):
            with self.assertRaises(ValidationError): purchases.purchase(self.user,**data)
        attempt.refresh_from_db(); self.assertEqual(attempt.status,'failed')

    def test_price_mutation_between_quote_and_prepare_never_builds_inconsistent_snapshot(self):
        data=self.intent()
        original=purchases.quote
        def changing_quote(*args,**kwargs):
            result=original(*args,**kwargs)
            Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=99,is_active=False)
            return result
        with patch('apps.gifts.services.purchases.quote',side_effect=changing_quote):
            with self.assertRaises(ValidationError): purchases.purchase(self.user,**data)
        self.assertFalse(GiftPurchase.objects.exists())
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_dispatch_audit_freezes_authorization_version(self):
        from apps.wallet.models import WalletSpendAudit
        record=purchases.purchase(self.user,**self.intent())
        detail=WalletSpendAudit.objects.get(attempt=record.attempt,event='dispatching').detail
        auth=detail['authorization']
        self.assertEqual(detail['authorization_digest'],spend_service.digest(auth))
        self.assertEqual(auth['price_version'],record.snapshot['price_version'])
        self.assertEqual(auth['policy_digest'],record.snapshot['policy_digest'])
        self.assertEqual(auth['buyer']['user_id'],self.user.pk)
        self.assertEqual(auth['buyer']['account_status'],self.profile.account_status)

    def test_prepared_with_dispatch_audit_cannot_release(self):
        from apps.wallet.models import WalletSpendAudit
        _,attempt=self.prepare()
        WalletSpendAudit.objects.create(attempt=attempt,event='dispatching',detail={})
        with self.assertRaises(ValidationError): spend_service.cancel_prepared(attempt.pk,user=self.user)
        attempt.refresh_from_db(); self.assertEqual(attempt.reserved_amount,attempt.amount)
        self.assertEqual(GiftPurchase.objects.get().status,'processing')

    def test_explicit_cancel_never_releases_unknown(self):
        _, attempt=self.prepare()
        result=spend_service.cancel_prepared(attempt.pk, user=self.user)
        self.assertEqual(result.status,'failed')
        self.assertEqual(result.reserved_amount,0)
        self.assertEqual(GiftPurchase.objects.get().status,'failed')
        other=spend_service.reserve(self.profile,kind='gift',business_no='second',key='second',amount=Decimal('1.00'),intent={})
        other.status='unknown'; other.save(update_fields=['status'])
        with self.assertRaises(ValidationError): spend_service.cancel_prepared(other.pk,user=self.user)
        other.refresh_from_db(); self.assertEqual(other.reserved_amount,Decimal('1'))
