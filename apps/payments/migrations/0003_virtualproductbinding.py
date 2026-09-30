import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0004_packageimage'),
        ('payments', '0002_refund'),
    ]

    operations = [
        migrations.CreateModel(
            name='VirtualProductBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('product_id', models.CharField(help_text='例如 escort_15。必须与微信虚拟支付后台中的道具ID完全一致。', max_length=20, unique=True, verbose_name='微信道具ID')),
                ('goods_price_fen', models.PositiveIntegerField(help_text='例如15元填写1500。', verbose_name='道具单价（分）')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('remark', models.CharField(blank=True, default='', max_length=100, verbose_name='备注')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('package', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='virtual_payment_bindings', to='catalog.package', verbose_name='绑定商品')),
                ('spec', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='virtual_payment_bindings', to='catalog.packagespec', verbose_name='绑定规格')),
            ],
            options={
                'verbose_name': '虚拟支付商品绑定',
                'verbose_name_plural': '虚拟支付商品绑定',
                'db_table': 'virtual_product_bindings',
                'ordering': ['product_id'],
            },
        ),
    ]
