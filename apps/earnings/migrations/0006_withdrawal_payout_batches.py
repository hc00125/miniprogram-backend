from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('earnings', '0005_review_days_3'),
    ]

    operations = [
        migrations.CreateModel(
            name='WithdrawalPayoutBatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('batch_no', models.CharField(db_index=True, max_length=40, unique=True, verbose_name='系统付款批次号')),
                ('payment_method', models.CharField(choices=[('wechat', '微信'), ('alipay', '支付宝'), ('bank', '银行卡'), ('other', '其他')], default='wechat', max_length=20, verbose_name='实际付款方式')),
                ('item_count', models.PositiveIntegerField(default=0, verbose_name='付款笔数')),
                ('total_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=14, verbose_name='付款总额')),
                ('external_transfer_no', models.CharField(blank=True, default='', help_text='选填。微信、支付宝或银行提供的外部流水号。', max_length=150, verbose_name='外部转账批次号')),
                ('admin_note', models.CharField(blank=True, default='', max_length=500, verbose_name='付款备注')),
                ('proof_url', models.CharField(blank=True, default='', max_length=500, verbose_name='付款凭证URL')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='付款时间')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_withdrawal_payout_batches', to=settings.AUTH_USER_MODEL, verbose_name='付款管理员')),
            ],
            options={
                'verbose_name': '提现付款批次',
                'verbose_name_plural': '提现付款批次',
                'db_table': 'withdrawal_payout_batches',
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='WithdrawalPayoutBatchItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount_snapshot', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='付款金额快照')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='items', to='earnings.withdrawalpayoutbatch', verbose_name='付款批次')),
                ('withdrawal', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='payout_batch_item', to='earnings.withdrawal', verbose_name='提现申请')),
            ],
            options={
                'verbose_name': '提现付款批次明细',
                'verbose_name_plural': '提现付款批次明细',
                'db_table': 'withdrawal_payout_batch_items',
                'ordering': ['id'],
            },
        ),
    ]
