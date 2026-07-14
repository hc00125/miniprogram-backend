from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def normalize_default_config(apps, schema_editor):
    EarningsConfig = apps.get_model('earnings', 'EarningsConfig')
    config, _created = EarningsConfig.objects.get_or_create(key='default')
    config.default_commission_rate = Decimal('16.00')
    config.min_withdrawal_amount = Decimal('10.00')
    config.save(update_fields=['default_commission_rate', 'min_withdrawal_amount'])


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('earnings', '0002_seed_default_config'),
    ]

    operations = [
        migrations.AlterField(
            model_name='earningsconfig',
            name='min_withdrawal_amount',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('10.00'),
                help_text='本字段单位为鱼干。当前10鱼干=1元。',
                max_digits=12,
                validators=[models.Min(Decimal('0.01'))] if False else [],
                verbose_name='最低提现鱼干',
            ),
        ),
        migrations.AddField(
            model_name='playerearning',
            name='debt_offset_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='抵扣欠款鱼干'),
        ),
        migrations.AddField(
            model_name='playerearning',
            name='reversed_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='已冲销鱼干'),
        ),
        migrations.AddField(
            model_name='playerwallet',
            name='debt_balance',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                help_text='罚款或退款冲销无法从可提现余额扣除时形成，后续工资会优先抵扣。',
                max_digits=12,
                verbose_name='待抵扣鱼干',
            ),
        ),
        migrations.AlterField(
            model_name='walletledger',
            name='bucket',
            field=models.CharField(
                choices=[
                    ('pending', '审核中'),
                    ('available', '可提现'),
                    ('withdrawing', '提现中'),
                    ('withdrawn', '已提现'),
                    ('debt', '待抵扣'),
                ],
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name='WalletAdjustment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('adjustment_type', models.CharField(choices=[('penalty', '罚款'), ('reward', '奖励/补发'), ('refund_reversal', '退款工资冲销')], db_index=True, max_length=30)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='调整鱼干')),
                ('available_delta', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('pending_delta', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('debt_delta', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('reason', models.CharField(max_length=500)),
                ('evidence_url', models.CharField(blank=True, default='', max_length=500)),
                ('reference_type', models.CharField(blank=True, default='', max_length=40)),
                ('reference_id', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('applied_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_wallet_adjustments', to=settings.AUTH_USER_MODEL)),
                ('order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='wallet_adjustments', to='orders.order')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='wallet_adjustments', to='players.player')),
            ],
            options={
                'verbose_name': '钱包奖惩与冲销',
                'verbose_name_plural': '钱包奖惩与冲销',
                'db_table': 'wallet_adjustments',
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.RemoveConstraint(
            model_name='playerearning',
            name='earning_amounts_non_negative',
        ),
        migrations.AddConstraint(
            model_name='playerearning',
            constraint=models.CheckConstraint(
                check=models.Q(
                    ('available_amount__gte', Decimal('0.00')),
                    ('commission_amount__gte', Decimal('0.00')),
                    ('debt_offset_amount__gte', Decimal('0.00')),
                    ('gross_amount__gte', Decimal('0.00')),
                    ('net_amount__gte', Decimal('0.00')),
                    ('reversed_amount__gte', Decimal('0.00')),
                    ('withdrawing_amount__gte', Decimal('0.00')),
                    ('withdrawn_amount__gte', Decimal('0.00')),
                ),
                name='earning_amounts_non_negative',
            ),
        ),
        migrations.AddConstraint(
            model_name='playerwallet',
            constraint=models.CheckConstraint(
                check=models.Q(
                    ('available_balance__gte', Decimal('0.00')),
                    ('debt_balance__gte', Decimal('0.00')),
                    ('pending_balance__gte', Decimal('0.00')),
                    ('withdrawing_balance__gte', Decimal('0.00')),
                    ('withdrawn_total__gte', Decimal('0.00')),
                ),
                name='wallet_balances_non_negative',
            ),
        ),
        migrations.AddConstraint(
            model_name='walletadjustment',
            constraint=models.CheckConstraint(check=models.Q(('amount__gt', Decimal('0.00'))), name='wallet_adjustment_amount_positive'),
        ),
        migrations.AddConstraint(
            model_name='walletadjustment',
            constraint=models.UniqueConstraint(
                condition=models.Q(('reference_id', ''), _negated=True),
                fields=('player', 'adjustment_type', 'reference_type', 'reference_id'),
                name='uniq_wallet_adjustment_reference',
            ),
        ),
        migrations.RunPython(normalize_default_config, migrations.RunPython.noop),
    ]
