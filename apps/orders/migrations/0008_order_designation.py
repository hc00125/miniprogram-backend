from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0007_order_renewal_fields'),
        ('players', '0006_playerapplication_real_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderDesignation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('pending', '待接受'), ('accepted', '已接受'), ('declined', '已拒绝'), ('expired', '已超时'), ('cancelled', '已取消')], db_index=True, default='pending', max_length=20)),
                ('extra_amount', models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=10, verbose_name='指定服务费')),
                ('invited_at', models.DateTimeField(auto_now_add=True)),
                ('responded_at', models.DateTimeField(blank=True, null=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='designations', to='orders.order', verbose_name='订单')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='order_designations', to='players.player', verbose_name='指定陪玩')),
            ],
            options={
                'verbose_name': '指定陪玩邀请',
                'verbose_name_plural': '指定陪玩邀请',
                'db_table': 'order_designations',
                'ordering': ['invited_at', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='orderdesignation',
            constraint=models.UniqueConstraint(fields=('order', 'player'), name='uniq_order_designated_player'),
        ),
    ]
