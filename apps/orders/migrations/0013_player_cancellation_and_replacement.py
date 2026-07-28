from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('earnings', '0004_default_commission_16'),
        ('orders', '0012_order_payment_window'),
        ('players', '0013_player_service_listing'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlayerDiscipline',
            fields=[
                ('player', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='discipline', serialize=False, to='players.player', verbose_name='陪玩师')),
                ('suspended_until', models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='暂停接单至')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': '陪玩接单限制',
                'verbose_name_plural': '陪玩接单限制',
                'db_table': 'player_discipline',
            },
        ),
        migrations.CreateModel(
            name='PlayerCancellationRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stage', models.CharField(choices=[('before_join', '进队前取消'), ('in_service', '进队后/服务中取消')], db_index=True, max_length=20)),
                ('replacement_mode', models.CharField(choices=[('public', '公开补位'), ('targeted', '老板重新指定')], max_length=20)),
                ('reason', models.CharField(max_length=500)),
                ('previous_order_status', models.CharField(blank=True, default='', max_length=20)),
                ('was_designated', models.BooleanField(default=False)),
                ('player_type_id_snapshot', models.PositiveBigIntegerField(blank=True, null=True)),
                ('player_type_name_snapshot', models.CharField(blank=True, default='', max_length=60)),
                ('booked_hours_snapshot', models.DecimalField(decimal_places=2, default=Decimal('1.00'), max_digits=8)),
                ('used_free_chance', models.BooleanField(default=False)),
                ('fine_rmb', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=8)),
                ('fine_fish', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('deducted_fish', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('debt_fish', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('suspended_until', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='player_cancellations', to='orders.order', verbose_name='订单')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cancellation_records', to='players.player', verbose_name='陪玩师')),
                ('wallet_adjustment', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='player_cancellation', to='earnings.walletadjustment')),
            ],
            options={
                'verbose_name': '陪玩取消记录',
                'verbose_name_plural': '陪玩取消记录',
                'db_table': 'player_cancellation_records',
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='OrderReplacementState',
            fields=[
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='replacement_state', serialize=False, to='orders.order', verbose_name='订单')),
                ('mode', models.CharField(choices=[('public', '公开补位'), ('targeted', '老板重新指定')], db_index=True, max_length=20)),
                ('status', models.CharField(choices=[('open', '待处理'), ('cancel_requested', '老板申请取消剩余服务'), ('resolved', '已解决')], db_index=True, default='open', max_length=30)),
                ('missing_slots', models.PositiveSmallIntegerField(default=1)),
                ('resume_status', models.CharField(blank=True, default='', max_length=20)),
                ('remaining_minutes', models.PositiveIntegerField(blank=True, null=True)),
                ('cancelled_player_name', models.CharField(blank=True, default='', max_length=100)),
                ('required_player_type_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('required_player_type_name', models.CharField(blank=True, default='', max_length=60)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('latest_cancellation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replacement_states', to='orders.playercancellationrecord')),
            ],
            options={
                'verbose_name': '订单补位状态',
                'verbose_name_plural': '订单补位状态',
                'db_table': 'order_replacement_states',
            },
        ),
    ]
