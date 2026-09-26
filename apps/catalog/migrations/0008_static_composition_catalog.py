import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0007_game_service_navigation'),
        ('players', '0011_player_minimum_designated_player_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='PackageFamily',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=80, verbose_name='装备商品族名称')),
                ('code', models.SlugField(help_text='稳定标识，例如 arena-four-sets-four-bullets。创建后不要随意修改。', max_length=80, unique=True, verbose_name='装备商品族代码')),
                ('description', models.CharField(blank=True, default='', max_length=300, verbose_name='说明')),
                ('sort_order', models.IntegerField(default=0, verbose_name='排序')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='是否启用')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('game_service', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='package_families', to='catalog.gameservice', verbose_name='所属游戏')),
            ],
            options={
                'verbose_name': '装备商品族',
                'verbose_name_plural': '装备商品族',
                'db_table': 'package_families',
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.AddField(
            model_name='package',
            name='package_family',
            field=models.ForeignKey(blank=True, help_text='将同一装备配置的 1/2/3 人套餐归入同一个商品族，用于指定陪玩组合计价。', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='packages', to='catalog.packagefamily', verbose_name='装备商品族'),
        ),
        migrations.AddConstraint(
            model_name='package',
            constraint=models.UniqueConstraint(condition=models.Q(package_family__isnull=False), fields=('package_family', 'player_count'), name='uniq_package_family_player_count'),
        ),
        migrations.CreateModel(
            name='PlayerOffer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='是否启用')),
                ('sort_order', models.IntegerField(default=0, verbose_name='排序')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('package_family', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='player_offers', to='catalog.packagefamily', verbose_name='装备商品族')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='player_offers', to='players.player', verbose_name='陪玩师')),
            ],
            options={
                'verbose_name': '陪玩可售装备',
                'verbose_name_plural': '陪玩可售装备',
                'db_table': 'player_offers',
                'ordering': ['sort_order', 'id'],
                'constraints': [models.UniqueConstraint(fields=('player', 'package_family'), name='uniq_player_package_family_offer')],
            },
        ),
        migrations.CreateModel(
            name='CompositionSku',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('required_players', models.PositiveSmallIntegerField(verbose_name='总人数')),
                ('designated_type_signature', models.JSONField(blank=True, default=list, help_text='规范格式：[{"player_type_id": 3, "count": 1}]，保存时按类型 ID 排序并合并。', verbose_name='指定计费类型签名')),
                ('composition_key', models.CharField(blank=True, db_index=True, editable=False, help_text='由装备商品族、基础类型、总人数和指定类型签名自动生成。', max_length=300, unique=True, verbose_name='组合键')),
                ('total_price_per_hour', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='组合每小时价格')),
                ('is_active', models.BooleanField(db_index=True, default=False, help_text='启用前必须补齐 1/2/3 人套餐、对应类型规格和微信虚拟支付绑定。', verbose_name='是否启用')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('base_player_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='base_composition_skus', to='catalog.playertype', verbose_name='基础陪玩类型')),
                ('package_family', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='composition_skus', to='catalog.packagefamily', verbose_name='装备商品族')),
                ('virtual_package_spec', models.OneToOneField(blank=True, help_text='仅用于微信虚拟支付绑定的内部规格；其价格必须等于组合每小时价格。', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='composition_virtual_sku', to='catalog.packagespec', verbose_name='内部虚拟支付规格')),
            ],
            options={
                'verbose_name': '指定陪玩组合 SKU',
                'verbose_name_plural': '指定陪玩组合 SKU',
                'db_table': 'composition_skus',
                'ordering': ['package_family__sort_order', 'required_players', 'id'],
            },
        ),
    ]
