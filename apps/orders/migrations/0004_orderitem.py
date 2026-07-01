# Generated for merged cart checkout order items

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0003_package_image_url_package_picture_url_and_more'),
        ('orders', '0003_cartitem'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('package_name', models.CharField(max_length=120)),
                ('spec_name', models.CharField(blank=True, default='', max_length=120)),
                ('spec_display_name', models.CharField(blank=True, default='', max_length=120)),
                ('unit_price', models.FloatField()),
                ('quantity', models.PositiveIntegerField(default=1)),
                ('amount', models.FloatField()),
                ('image_url', models.CharField(blank=True, max_length=500, null=True)),
                ('description', models.CharField(blank=True, max_length=300, null=True)),
                ('sort_order', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='orders.order')),
                ('package', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='order_items', to='catalog.package')),
                ('spec', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='order_items', to='catalog.packagespec')),
            ],
            options={
                'verbose_name': '订单商品',
                'verbose_name_plural': '订单商品列表',
                'db_table': 'order_items',
                'ordering': ['sort_order', 'id'],
            },
        ),
    ]
