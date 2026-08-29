import django.db.models.deletion
from django.db import migrations, models


def seed_game_services(apps, schema_editor):
    GameService = apps.get_model('catalog', 'GameService')
    PackageGroup = apps.get_model('catalog', 'PackageGroup')

    arena, _ = GameService.objects.update_or_create(
        code='arena-breakout',
        defaults={
            'name': '暗区突围',
            'sort_order': 10,
            'is_active': True,
        },
    )
    GameService.objects.update_or_create(
        code='delta-force',
        defaults={
            'name': '三角洲行动',
            'sort_order': 20,
            'is_active': True,
        },
    )
    PackageGroup.objects.filter(game_service__isnull=True).update(game_service=arena)


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_package_requires_escort_qualification'),
    ]

    operations = [
        migrations.CreateModel(
            name='GameService',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50, verbose_name='游戏名称')),
                ('code', models.CharField(max_length=50, unique=True, verbose_name='游戏代码')),
                ('icon', models.ImageField(blank=True, help_text='建议上传256×256或512×512的方形PNG/JPG，前端会裁剪为圆形。', null=True, upload_to='game-services/%Y/%m/', verbose_name='游戏图标')),
                ('icon_url', models.CharField(blank=True, default='', help_text='可选。已在CDN/OSS上的图标可填写完整URL；上传图标优先。', max_length=500, verbose_name='图标外链')),
                ('sort_order', models.IntegerField(default=0, verbose_name='排序')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='是否展示')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': '游戏服务',
                'verbose_name_plural': '游戏服务',
                'db_table': 'game_services',
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.AddField(
            model_name='packagegroup',
            name='game_service',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='package_groups', to='catalog.gameservice', verbose_name='所属游戏'),
        ),
        migrations.RunPython(seed_game_services, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='packagegroup',
            name='game_service',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='package_groups', to='catalog.gameservice', verbose_name='所属游戏'),
        ),
    ]
