from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


DEFAULT_TIERS = [
    {
        'code': 'member',
        'name': '普通会员',
        'min_consumption': Decimal('0.00'),
        'benefits': ['累计消费记录', '会员身份标识'],
        'badge_color': 'green',
        'sort_order': 0,
    },
    {
        'code': 'silver',
        'name': '白银VIP',
        'min_consumption': Decimal('300.00'),
        'benefits': ['专属VIP标识', '客服优先响应', '活动优先通知'],
        'badge_color': 'silver',
        'sort_order': 10,
    },
    {
        'code': 'gold',
        'name': '黄金VIP',
        'min_consumption': Decimal('1000.00'),
        'benefits': ['黄金VIP标识', '客服优先响应', '新品套餐优先体验'],
        'badge_color': 'gold',
        'sort_order': 20,
    },
    {
        'code': 'black_gold',
        'name': '黑金VIP',
        'min_consumption': Decimal('3000.00'),
        'benefits': ['黑金VIP标识', '专属客服通道', '重点活动优先参与'],
        'badge_color': 'black',
        'sort_order': 30,
    },
]


def seed_vip_tiers(apps, schema_editor):
    VipTier = apps.get_model('accounts', 'VipTier')
    ClientProfile = apps.get_model('accounts', 'ClientProfile')
    for payload in DEFAULT_TIERS:
        tier, _created = VipTier.objects.update_or_create(
            code=payload['code'],
            defaults=payload,
        )
        if payload['code'] == 'member':
            ClientProfile.objects.filter(vip_tier__isnull=True).update(vip_tier=tier)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounts', '0002_add_nickname_customized'),
        ('orders', '0009_orderplayer_room_entry'),
    ]

    operations = [
        migrations.CreateModel(
            name='VipTier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=30, unique=True, verbose_name='等级代码')),
                ('name', models.CharField(max_length=50, verbose_name='等级名称')),
                ('min_consumption', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='最低累计消费(元)')),
                ('benefits', models.JSONField(blank=True, default=list, verbose_name='会员权益')),
                ('badge_color', models.CharField(default='green', max_length=20, verbose_name='徽章主题')),
                ('sort_order', models.IntegerField(default=0, verbose_name='排序')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': '老板VIP等级',
                'verbose_name_plural': '老板VIP等级',
                'db_table': 'vip_tiers',
                'ordering': ['min_consumption', 'sort_order', 'id'],
            },
        ),
        migrations.AddField(
            model_name='clientprofile',
            name='cumulative_consumption',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='累计有效消费(元)'),
        ),
        migrations.AddField(
            model_name='clientprofile',
            name='vip_tier',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='members', to='accounts.viptier', verbose_name='当前VIP等级'),
        ),
        migrations.AddField(
            model_name='clientprofile',
            name='vip_updated_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='VIP更新时间'),
        ),
        migrations.CreateModel(
            name='BossConsumptionLedger',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='消费变动(元)')),
                ('balance_after', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='变动后累计消费(元)')),
                ('source_type', models.CharField(choices=[('order', '订单消费'), ('refund', '退款扣减'), ('manual', '人工调整'), ('backfill', '历史补录')], db_index=True, max_length=20, verbose_name='来源')),
                ('reference_id', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='来源编号')),
                ('reason', models.CharField(blank=True, default='', max_length=500, verbose_name='原因/备注')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('operator', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='boss_consumption_operations', to=settings.AUTH_USER_MODEL, verbose_name='操作管理员')),
                ('order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='boss_consumption_ledgers', to='orders.order', verbose_name='关联订单')),
                ('profile', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='consumption_ledgers', to='accounts.clientprofile', verbose_name='老板')),
            ],
            options={
                'verbose_name': '老板消费流水',
                'verbose_name_plural': '老板消费流水',
                'db_table': 'boss_consumption_ledgers',
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='viptier',
            constraint=models.CheckConstraint(check=Q(min_consumption__gte=Decimal('0.00')), name='vip_tier_min_consumption_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='clientprofile',
            constraint=models.CheckConstraint(check=Q(cumulative_consumption__gte=Decimal('0.00')), name='client_consumption_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='bossconsumptionledger',
            constraint=models.CheckConstraint(check=~Q(amount=Decimal('0.00')), name='boss_consumption_amount_non_zero'),
        ),
        migrations.AddConstraint(
            model_name='bossconsumptionledger',
            constraint=models.CheckConstraint(check=Q(balance_after__gte=Decimal('0.00')), name='boss_consumption_balance_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='bossconsumptionledger',
            constraint=models.UniqueConstraint(fields=('profile', 'source_type', 'reference_id'), condition=~Q(reference_id=''), name='uniq_boss_consumption_reference'),
        ),
        migrations.RunPython(seed_vip_tiers, migrations.RunPython.noop),
    ]
