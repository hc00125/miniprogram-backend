from django.db import migrations


def repair_paid_public_replacement_status(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    OrderPlayer = apps.get_model('orders', 'OrderPlayer')
    OrderReplacementState = apps.get_model('orders', 'OrderReplacementState')
    OrderMatchingWindow = apps.get_model('orders', 'OrderMatchingWindow')
    using = schema_editor.connection.alias

    orders = Order.objects.using(using).filter(
        order_type='normal',
        fulfillment_mode='public',
        paid=True,
        status__in=['待接单', '待支付'],
    ).iterator(chunk_size=500)

    for order in orders:
        current_players = OrderPlayer.objects.using(using).filter(order_id=order.pk).count()
        required_players = max(1, int(order.required_players or 1))
        missing_slots = max(0, required_players - current_players)
        resume_status = '进行中' if order.timer_started_at else '待开打'

        if missing_slots:
            state, _ = OrderReplacementState.objects.using(using).get_or_create(
                order_id=order.pk,
                defaults={
                    'mode': 'public',
                    'status': 'open',
                    'missing_slots': missing_slots,
                    'resume_status': resume_status,
                    'remaining_minutes': max(1, int(float(order.booked_hours or 1) * 60)),
                    'cancelled_player_name': '',
                    'required_player_type_name': '',
                },
            )
            state.mode = 'public'
            state.status = 'open'
            state.missing_slots = missing_slots
            state.resume_status = resume_status
            if not state.remaining_minutes:
                state.remaining_minutes = max(1, int(float(order.booked_hours or 1) * 60))
            state.current_designation_id = None
            state.resolved_at = None
            state.save(using=using)
        else:
            OrderReplacementState.objects.using(using).filter(
                order_id=order.pk,
                status='open',
            ).update(status='resolved', missing_slots=0)

        Order.objects.using(using).filter(pk=order.pk).update(status=resume_status)
        OrderMatchingWindow.objects.using(using).filter(order_id=order.pk).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0014_order_matching_windows'),
    ]

    operations = [
        migrations.RunPython(
            repair_paid_public_replacement_status,
            migrations.RunPython.noop,
        ),
    ]
