from django.db import migrations


def clear_room_entry_deadlines(apps, schema_editor):
    OrderPlayer = apps.get_model('orders', 'OrderPlayer')
    using = schema_editor.connection.alias

    # 新规则不再限制付款后10分钟进入房间。
    OrderPlayer.objects.using(using).filter(room_join_deadline__isnull=False).update(
        room_join_deadline=None,
    )
    OrderPlayer.objects.using(using).filter(room_join_status='overdue').update(
        room_join_status='pending',
    )
    OrderPlayer.objects.using(using).filter(room_join_status='late_confirmed').update(
        room_join_status='confirmed',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0015_repair_paid_public_replacement_status'),
    ]

    operations = [
        migrations.RunPython(clear_room_entry_deadlines, migrations.RunPython.noop),
    ]
