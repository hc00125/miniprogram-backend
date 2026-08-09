from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations


CENT = Decimal('0.01')
CREDIT_TYPE = 'virtual_purchase_credit'
SPEND_TYPE = 'virtual_order_payment'


def backfill_virtual_payment_diamond_bridges(apps, schema_editor):
    Payment = apps.get_model('payments', 'Payment')
    Order = apps.get_model('orders', 'Order')
    ClientProfile = apps.get_model('accounts', 'ClientProfile')
    ClientWallet = apps.get_model('wallet', 'ClientWallet')
    ClientWalletLedger = apps.get_model('wallet', 'ClientWalletLedger')

    using = schema_editor.connection.alias
    payments = (
        Payment.objects.using(using)
        .filter(channel='wechat_virtual', status__in=['paid', 'refunded'])
        .order_by('created_at', 'id')
        .iterator(chunk_size=500)
    )

    for payment in payments:
        order = (
            Order.objects.using(using)
            .filter(order_no=payment.order_id)
            .values('boss_user_id')
            .first()
        )
        if not order or not order['boss_user_id']:
            continue

        profile = (
            ClientProfile.objects.using(using)
            .filter(user_id=order['boss_user_id'])
            .values('id')
            .first()
        )
        if not profile:
            continue

        wallet, _created = ClientWallet.objects.using(using).get_or_create(profile_id=profile['id'])
        amount = Decimal(str(payment.amount or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
        if amount <= 0:
            continue

        credit = ClientWalletLedger.objects.using(using).filter(
            wallet_id=wallet.id,
            entry_type=CREDIT_TYPE,
            reference_id=payment.payment_no,
        ).first()
        spend = ClientWalletLedger.objects.using(using).filter(
            wallet_id=wallet.id,
            entry_type=SPEND_TYPE,
            reference_id=payment.payment_no,
        ).first()

        # 新逻辑始终在一个事务里同时写入两条，因此历史回填只处理“都没有”的旧单。
        # 如果只存在一条，说明数据曾被人工修改，留给管理员核对，避免迁移猜测余额。
        if bool(credit) != bool(spend):
            continue

        if not credit:
            original_balance = Decimal(str(wallet.balance or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
            credit = ClientWalletLedger.objects.using(using).create(
                wallet_id=wallet.id,
                entry_type=CREDIT_TYPE,
                amount=amount,
                balance_after=original_balance + amount,
                reference_type='payment',
                reference_id=payment.payment_no,
                note=f'历史微信直付 {payment.payment_no}：补录人民币自动兑换钻石（内部结算）',
            )
            spend = ClientWalletLedger.objects.using(using).create(
                wallet_id=wallet.id,
                entry_type=SPEND_TYPE,
                amount=-amount,
                balance_after=original_balance,
                reference_type='payment',
                reference_id=payment.payment_no,
                note=f'历史微信直付 {payment.payment_no}：补录钻石支付订单 {payment.order_id}（内部结算）',
            )

        payload = payment.notify_payload if isinstance(payment.notify_payload, dict) else {}
        settlement = payload.get('diamond_settlement') or {}
        if not isinstance(settlement, dict):
            settlement = {}
        settlement.update({
            'status': 'settled',
            'amount_yuan': str(amount),
            'diamonds': int(amount * 10),
            'purchase_credit_ledger_id': credit.id,
            'order_payment_ledger_id': spend.id,
            'purchase_credit_reference': payment.payment_no,
            'order_payment_reference': payment.payment_no,
            'backfilled': True,
        })
        Payment.objects.using(using).filter(pk=payment.pk).update(
            notify_payload={**payload, 'diamond_settlement': settlement},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0006_unified_diamond_ledger_types'),
        ('payments', '0005_decimal_payment_amounts'),
        ('orders', '0015_repair_paid_public_replacement_status'),
    ]

    operations = [
        migrations.RunPython(backfill_virtual_payment_diamond_bridges, migrations.RunPython.noop),
    ]
