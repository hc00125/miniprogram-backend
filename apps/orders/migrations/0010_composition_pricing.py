# Generated for static designated-play composition pricing.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        # Composition-priced orders are only meaningful after the static
        # catalog models (PackageFamily/CompositionSku/PlayerOffer) exist.
        ('catalog', '0008_static_composition_catalog'),
        ('orders', '0009_orderplayer_room_entry'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='composition_key',
            field=models.CharField(blank=True, default='', max_length=180, verbose_name='组合结算SKU键快照'),
        ),
        migrations.AddField(
            model_name='order',
            name='composition_price_per_hour',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='组合每小时价格快照'),
        ),
        migrations.AddField(
            model_name='order',
            name='composition_pricing_error',
            field=models.CharField(blank=True, default='', max_length=300, verbose_name='组合定价配置错误'),
        ),
        migrations.AddField(
            model_name='order',
            name='composition_sku_id',
            field=models.PositiveBigIntegerField(blank=True, db_index=True, null=True, verbose_name='组合结算SKU ID快照'),
        ),
        migrations.AddField(
            model_name='order',
            name='composition_virtual_spec_id',
            field=models.PositiveBigIntegerField(blank=True, null=True, verbose_name='组合虚拟商品规格ID快照'),
        ),
        migrations.AddField(
            model_name='order',
            name='pricing_mode',
            field=models.CharField(choices=[('legacy', '普通定价'), ('composition', '指定陪玩组合定价')], db_index=True, default='legacy', max_length=20, verbose_name='定价模式'),
        ),
        migrations.CreateModel(
            name='OrderPricingLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(choices=[('public', '公开抢单名额'), ('designated', '指定陪玩名额')], max_length=20)),
                ('player_id_snapshot', models.PositiveBigIntegerField(blank=True, null=True)),
                ('player_name_snapshot', models.CharField(blank=True, default='', max_length=100)),
                ('billing_player_type_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('billing_player_type_name', models.CharField(blank=True, default='', max_length=60)),
                ('package_id_snapshot', models.PositiveBigIntegerField(blank=True, null=True)),
                ('package_name_snapshot', models.CharField(blank=True, default='', max_length=120)),
                ('spec_id_snapshot', models.PositiveBigIntegerField(blank=True, null=True)),
                ('spec_name_snapshot', models.CharField(blank=True, default='', max_length=120)),
                ('quantity', models.PositiveSmallIntegerField(default=1)),
                ('unit_price', models.DecimalField(decimal_places=2, max_digits=10)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('sort_order', models.PositiveSmallIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pricing_lines', to='orders.order')),
            ],
            options={
                'verbose_name': '订单价格明细',
                'verbose_name_plural': '订单价格明细',
                'db_table': 'order_pricing_lines',
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.CreateModel(
            name='DesignatedOrderDraft',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('boss_wechat', models.CharField(max_length=50)),
                ('game_id', models.CharField(blank=True, max_length=100, null=True)),
                ('designated_player_ids', models.JSONField(blank=True, default=list)),
                ('booked_hours', models.PositiveSmallIntegerField(default=1)),
                ('boss_note', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('draft', '编辑中'), ('submitted', '已提交')], db_index=True, default='draft', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('base_spec', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='designated_order_drafts', to='catalog.packagespec')),
                ('boss_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='designated_order_drafts', to=settings.AUTH_USER_MODEL)),
                ('package', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='designated_order_drafts', to='catalog.package')),
                ('submitted_order', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='designated_draft', to='orders.order')),
            ],
            options={
                'verbose_name': '指定陪玩草稿',
                'verbose_name_plural': '指定陪玩草稿',
                'db_table': 'designated_order_drafts',
                'ordering': ['-updated_at', '-id'],
            },
        ),
    ]
