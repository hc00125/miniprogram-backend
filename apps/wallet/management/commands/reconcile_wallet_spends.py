from django.core.management.base import BaseCommand, CommandError
from apps.wallet.models import WalletSpendAttempt, WalletSpendAudit
from apps.wallet.spend_service import recover


class Command(BaseCommand):
    help = 'Default read-only. --apply cancels undispatched prepared, queries unknown, or finalizes succeeded; never reissues currency_pay.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--attempt-id', type=int)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 1000:
            raise CommandError('limit must be 1..1000')
        if not options['attempt_id']:
            from apps.wallet.spend_models import OrderWalletSpend
            for binding in OrderWalletSpend.objects.exclude(blocker='').order_by('id')[:options['limit']]:
                self.stdout.write(f'order={binding.order_id} manual_blocked {binding.blocker}')
        qs = WalletSpendAttempt.objects.filter(status__in=('prepared', 'dispatching', 'unknown', 'succeeded')).order_by('id')
        if options['attempt_id']:
            qs = qs.filter(pk=options['attempt_id'])
        for attempt in list(qs[:options['limit']]):
            if not options['apply']:
                self.stdout.write(f'{attempt.pk} {attempt.kind} {attempt.status} {attempt.blocker}')
                continue
            try:
                if attempt.kind == 'gift':
                    from apps.gifts.models import GiftPurchase
                    from apps.gifts.services.purchases import fulfill
                    if not GiftPurchase.objects.filter(attempt=attempt).exists():
                        raise ValueError('ORPHAN_BUSINESS_RECORD')
                elif attempt.kind == 'surcharge':
                    from apps.orders.surcharge_models import OrderSurcharge
                    from apps.orders.surcharge_payment import fulfill
                    if not OrderSurcharge.objects.filter(attempt=attempt).exists():
                        raise ValueError('ORPHAN_BUSINESS_RECORD')
                elif attempt.kind == 'order_checkout':
                    from apps.orders.surcharge_models import OrderCheckout
                    from apps.orders.checkout_payment import fulfill
                    if not OrderCheckout.objects.filter(attempt=attempt).exists():
                        raise ValueError('ORPHAN_BUSINESS_RECORD')
                elif attempt.kind == 'order':
                    from apps.wallet.spend_models import OrderWalletSpend
                    from apps.wallet.order_spend import fulfill
                    if not OrderWalletSpend.objects.filter(attempt=attempt, blocker='').exists():
                        raise ValueError('ORPHAN_BUSINESS_RECORD')
                else:
                    raise ValueError('UNKNOWN_BUSINESS_KIND')
                result = recover(attempt.pk, apply=fulfill)
                self.stdout.write(f'{attempt.pk} {result.status} {result.blocker}')
            except Exception as exc:
                # No secrets or raw upstream payloads in logs/audit.
                WalletSpendAudit.objects.create(attempt=attempt, event='recovery_blocked',
                    detail={'error_type': type(exc).__name__})
                self.stderr.write(f'{attempt.pk} recovery_blocked {type(exc).__name__}')
