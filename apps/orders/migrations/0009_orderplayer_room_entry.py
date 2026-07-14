from datetime import timedelta

from django.db import migrations, models


def backfill_room_entry_deadlines(apps, schema_editor):
    OrderPlayer = apps.get_model('orders', 'OrderPlayer')
    finished_statuses = {'已完成', '已取消'}
    for item in OrderPlayer.objects.select_related('order').filter(room_join_deadline__isnull=True).iterator():
        item.room_join_deadline = item.grab_time + timedelta(minutes=10) if item.grab_time else None
        update_fields = ['room_join_deadline']
        if item.order.status in finished_statuses:
            item.room_join_status = 'waived'
            update_fields.append('room_join_status')
        item.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0008_order_designation'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderplayer',
            name='room_join_confirmed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='确认进入房间时间'),
        ),
        migrations.AddField(
            model_name='orderplayer',
            name='room_join_deadline',
            field=models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='进入房间截止时间'),
        ),
        migrations.AddField(
            model_name='orderplayer',
            name='room_join_status',
            field=models.CharField(choices=[('pending', '等待进入'), ('confirmed', '按时进入'), ('late_confirmed', '超时后进入'), ('overdue', '已超时待核实'), ('waived', '管理员免除')], db_index=True, default='pending', max_length=20, verbose_name='进入房间状态'),
        ),
        migrations.RunPython(backfill_room_entry_deadlines, migrations.RunPython.noop),
    ]
