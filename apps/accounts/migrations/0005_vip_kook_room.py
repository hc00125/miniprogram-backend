from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


PRIVATE_KOOK_ROOM_FEATURE = 'private_kook_room'
PRIVATE_KOOK_ROOM_TIER_CODES = [
    'silver_mouse',
    'gold_mouse',
    'platinum_mouse',
    'emerald_mouse',
    'diamond_mouse',
    'glory_mouse',
    'brilliant_mouse',
    'dream_mouse',
    'elegant_mouse',
    'supreme_mouse',
]


def seed_private_kook_room_feature(apps, schema_editor):
    VipTier = apps.get_model('accounts', 'VipTier')

    for tier in VipTier.objects.all().iterator():
        feature_codes = list(tier.feature_codes or [])
        benefits = list(tier.benefits or [])

        if tier.code in PRIVATE_KOOK_ROOM_TIER_CODES:
            if PRIVATE_KOOK_ROOM_FEATURE not in feature_codes:
                feature_codes.append(PRIVATE_KOOK_ROOM_FEATURE)
            if '专属KOOK房间' not in benefits:
                benefits.append('专属KOOK房间')
        else:
            feature_codes = [
                code for code in feature_codes
                if code != PRIVATE_KOOK_ROOM_FEATURE
            ]

        VipTier.objects.filter(pk=tier.pk).update(
            feature_codes=feature_codes,
            benefits=benefits,
        )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounts', '0004_mouse_vip_tiers'),
    ]

    operations = [
        migrations.AddField(
            model_name='viptier',
            name='feature_codes',
            field=models.JSONField(blank=True, default=list, verbose_name='可执行权益代码'),
        ),
        migrations.CreateModel(
            name='ClientVipKookRoom',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kook_room_number', models.CharField(blank=True, default='', max_length=100, verbose_name='专属KOOK房间号')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('assigned_at', models.DateTimeField(blank=True, null=True, verbose_name='配置时间')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assigned_vip_kook_rooms', to=settings.AUTH_USER_MODEL, verbose_name='配置超管')),
                ('profile', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='vip_kook_room', to='accounts.clientprofile', verbose_name='老板')),
            ],
            options={
                'verbose_name': '老板专属KOOK房间',
                'verbose_name_plural': '老板专属KOOK房间',
                'db_table': 'client_vip_kook_rooms',
                'ordering': ['-updated_at', '-id'],
            },
        ),
        migrations.RunPython(seed_private_kook_room_feature, migrations.RunPython.noop),
    ]
