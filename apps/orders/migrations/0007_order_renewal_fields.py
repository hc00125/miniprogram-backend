import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0006_order_ready_to_start_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='order_type',
            field=models.CharField(
                choices=[('normal', '普通订单'), ('renewal', '续单')],
                db_index=True,
                default='normal',
                max_length=20,
                verbose_name='订单类型',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='parent_order',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='renewal_orders',
                to='orders.order',
                verbose_name='原订单',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='renewal_index',
            field=models.PositiveIntegerField(default=0, verbose_name='续单序号'),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.UniqueConstraint(
                condition=Q(parent_order__isnull=False),
                fields=('parent_order', 'renewal_index'),
                name='uniq_order_renewal_index',
            ),
        ),
    ]
