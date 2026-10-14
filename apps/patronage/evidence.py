"""Trusted internal shared-debit evidence; never accepts a caller's paid flag."""
from apps.wallet.models import ClientWallet, ClientWalletLedger, WalletSpendAttempt
from apps.wallet.spend_service import fail
from .models import PatronagePurchase


def lock_confirmed_payment(purchase_id, *, require_paid=True):
    """Inside atomic: client wallet -> attempt -> purchase. Never reverse it.

    `completed` and the matching real shared-spend debit are required together.
    A succeeded external request alone is not permission to credit local income.
    """
    purchase = PatronagePurchase.objects.get(pk=purchase_id)
    if not purchase.attempt_id:
        fail('PATRONAGE_PAYMENT_UNCONFIRMED')
    attempt = WalletSpendAttempt.objects.get(pk=purchase.attempt_id)
    buyer_wallet = ClientWallet.objects.select_for_update().get(pk=attempt.wallet_id)
    attempt = WalletSpendAttempt.objects.select_for_update().get(pk=attempt.pk)
    purchase = PatronagePurchase.objects.select_for_update().get(pk=purchase.pk)
    allowed_states = ('paid',) if require_paid else ('created', 'processing', 'unknown', 'paid')
    if (purchase.payment_status not in allowed_states
            or (purchase.payment_status == 'paid' and purchase.paid_at is None)
            or purchase.attempt_id != attempt.pk or attempt.status != 'completed'
            or attempt.reserved_amount != 0 or attempt.kind != 'patronage'
            or attempt.business_no != purchase.purchase_no
            or attempt.idempotency_key != purchase.idempotency_key
            or attempt.amount != purchase.amount_yuan
            or buyer_wallet.profile.user_id != purchase.boss_id):
        fail('PATRONAGE_PAYMENT_UNCONFIRMED')
    debit = ClientWalletLedger.objects.filter(wallet=buyer_wallet, entry_type='shared_spend',
        reference_type='patronage', reference_id=attempt.external_id,
        amount=-purchase.amount_yuan).first()
    if debit is None:
        fail('PATRONAGE_PAYMENT_UNCONFIRMED')
    return purchase, attempt, debit
