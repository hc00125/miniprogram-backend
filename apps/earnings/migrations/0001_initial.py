import decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('orders', '0007_order_renewal_fields'),
        ('players', '0006_playerapplication_real_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='EarningsConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(default='default', max_length=30, unique=True)),
                ('default_commission_rate', models.DecimalField(decimal_places=2, default=decimal.Decimal('15.00'), max_digits=5, validators=[django.core.validators.MinValueValidator(decimal.Decimal('0.00')), django.core.validators.MaxValueValidator(decimal.Decimal('100.00'))], verbose_name='默认抽成比例(%)')),
                ('review_days', models.PositiveSmallIntegerField(default=8, verbose_name='工资审核天数')),
                ('min_withdrawal_amount', models.DecimalField(decimal_places=2, default=decimal.Decimal('1.00'), max_digits=12, validators=[django.core.validators.MinValueValidator(decimal.Decimal('0.01'))], verbose_name='最低提现金额')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_earnings_configs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': '收益配置',
                'verbose_name_plural': '收益配置',
                'db_table': 'earnings_config',
            },
        ),
        migrations.CreateModel(
            name='PlayerWallet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('pending_balance', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12, verbose_name='审核中鱼干')),
                ('available_balance', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12, verbose_name='可提现鱼干')),
                ('withdrawing_balance', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12, verbose_name='提现中鱼干')),
                ('withdrawn_total', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12, verbose_name='累计已提现')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('player', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='wallet', to='players.player', verbose_name='陪玩师')),
            ],
            options={
                'verbose_name': '陪玩钱包',
                'verbose_name_plural': '陪玩钱包',
                'db_table': 'player_wallets',
            },
        ),
        migrations.CreateModel(
            name='OrderCommissionOverride',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('commission_rate', models.DecimalField(decimal_places=2, max_digits=5, validators=[django.core.validators.MinValueValidator(decimal.Decimal('0.00')), django.core.validators.MaxValueValidator(decimal.Decimal('100.00'))], verbose_name='订单抽成比例(%)')),
                ('reason', models.CharField(max_length=300, verbose_name='调整原因')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_commission_overrides', to=settings.AUTH_USER_MODEL)),
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='commission_override', to='orders.order', verbose_name='订单')),
            ],
            options={
                'verbose_name': '订单抽成设置',
                'verbose_name_plural': '订单抽成设置',
                'db_table': 'order_commission_overrides',
            },
        ),
        migrations.CreateModel(
            name='PlayerEarning',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('gross_amount', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='分配收入')),
                ('commission_rate', models.DecimalField(decimal_places=2, max_digits=5, verbose_name='抽成比例(%)')),
                ('commission_amount', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='平台抽成')),
                ('net_amount', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='应得鱼干')),
                ('status', models.CharField(choices=[('pending', '审核中'), ('available', '已审核'), ('frozen', '已冻结'), ('reversed', '已冲销')], db_index=True, default='pending', max_length=20)),
                ('review_until', models.DateTimeField(db_index=True, verbose_name='预计审核完成时间')),
                ('available_at', models.DateTimeField(blank=True, null=True, verbose_name='转为可提现时间')),
                ('available_amount', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12, verbose_name='当前可提现')),
                ('withdrawing_amount', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12, verbose_name='当前提现中')),
                ('withdrawn_amount', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12, verbose_name='累计已提现')),
                ('freeze_reason', models.CharField(blank=True, default='', max_length=300, verbose_name='冻结原因')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='player_earnings', to='orders.order', verbose_name='原订单')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='earnings', to='players.player', verbose_name='陪玩师')),
            ],
            options={
                'verbose_name': '订单工资',
                'verbose_name_plural': '订单工资',
                'db_table': 'player_earnings',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='Withdrawal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('withdrawal_no', models.CharField(db_index=True, max_length=32, unique=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(decimal.Decimal('0.01'))])),
                ('status', models.CharField(choices=[('pending_review', '待审核'), ('pending_payment', '待打款'), ('paid', '已打款'), ('rejected', '已驳回'), ('failed', '打款失败')], db_index=True, default='pending_review', max_length=30)),
                ('payment_method', models.CharField(choices=[('wechat', '微信'), ('alipay', '支付宝'), ('bank', '银行卡'), ('other', '其他')], default='wechat', max_length=20)),
                ('account_name', models.CharField(max_length=80, verbose_name='收款人')),
                ('account_no', models.CharField(max_length=150, verbose_name='收款账号')),
                ('request_note', models.CharField(blank=True, default='', max_length=300)),
                ('reject_reason', models.CharField(blank=True, default='', max_length=300)),
                ('admin_note', models.CharField(blank=True, default='', max_length=500)),
                ('transfer_no', models.CharField(blank=True, default='', max_length=150, verbose_name='转账流水号')),
                ('proof_url', models.CharField(blank=True, default='', max_length=500, verbose_name='打款凭证URL')),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('paid_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('paid_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='paid_withdrawals', to=settings.AUTH_USER_MODEL)),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='withdrawals', to='players.player')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_withdrawals', to=settings.AUTH_USER_MODEL)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='withdrawals', to='earnings.playerwallet')),
            ],
            options={
                'verbose_name': '提现申请',
                'verbose_name_plural': '提现申请',
                'db_table': 'withdrawals',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='WalletLedger',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('entry_type', models.CharField(choices=[('earning_created', '工资进入审核'), ('earning_released', '工资审核通过'), ('earning_adjusted', '工资金额调整'), ('earning_reversed', '工资冲销'), ('withdrawal_frozen', '提现冻结'), ('withdrawal_refunded', '提现退回'), ('withdrawal_paid', '提现完成'), ('admin_adjustment', '管理员调整')], db_index=True, max_length=40)),
                ('bucket', models.CharField(choices=[('pending', '审核中'), ('available', '可提现'), ('withdrawing', '提现中'), ('withdrawn', '已提现')], max_length=20)),
                ('delta', models.DecimalField(decimal_places=2, max_digits=12)),
                ('balance_after', models.DecimalField(decimal_places=2, max_digits=12)),
                ('reference_type', models.CharField(blank=True, default='', max_length=40)),
                ('reference_id', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('note', models.CharField(blank=True, default='', max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('operator', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='wallet_ledger_operations', to=settings.AUTH_USER_MODEL)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ledger_entries', to='earnings.playerwallet')),
            ],
            options={
                'verbose_name': '钱包流水',
                'verbose_name_plural': '钱包流水',
                'db_table': 'wallet_ledger',
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='WithdrawalAllocation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(decimal.Decimal('0.01'))])),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('earning', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='withdrawal_allocations', to='earnings.playerearning')),
                ('withdrawal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='allocations', to='earnings.withdrawal')),
            ],
            options={
                'verbose_name': '提现工资分配',
                'verbose_name_plural': '提现工资分配',
                'db_table': 'withdrawal_allocations',
            },
        ),
        migrations.AddConstraint(
            model_name='playerearning',
            constraint=models.UniqueConstraint(fields=('order', 'player'), name='uniq_order_player_earning'),
        ),
        migrations.AddConstraint(
            model_name='playerearning',
            constraint=models.CheckConstraint(condition=models.Q(('available_amount__gte', decimal.Decimal('0.00')), ('commission_amount__gte', decimal.Decimal('0.00')), ('gross_amount__gte', decimal.Decimal('0.00')), ('net_amount__gte', decimal.Decimal('0.00')), ('withdrawing_amount__gte', decimal.Decimal('0.00')), ('withdrawn_amount__gte', decimal.Decimal('0.00'))), name='earning_amounts_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='withdrawalallocation',
            constraint=models.UniqueConstraint(fields=('withdrawal', 'earning'), name='uniq_withdrawal_earning_allocation'),
        ),
    ]
