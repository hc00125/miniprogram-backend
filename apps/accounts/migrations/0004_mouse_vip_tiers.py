from decimal import Decimal

from django.db import migrations


MOUSE_VIP_TIERS = [
    {
        'code': 'mouse',
        'name': '鼠鼠',
        'min_consumption': Decimal('0.00'),
        'benefits': ['累计钻石记录', '鼠鼠身份标识'],
        'badge_color': 'mouse',
        'sort_order': 0,
        'is_active': True,
    },
    {
        'code': 'bronze_mouse',
        'name': '青铜鼠鼠',
        'min_consumption': Decimal('1.00'),
        'benefits': ['青铜鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'bronze',
        'sort_order': 10,
        'is_active': True,
    },
    {
        'code': 'silver_mouse',
        'name': '白银鼠鼠',
        'min_consumption': Decimal('2000.00'),
        'benefits': ['白银鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'silver',
        'sort_order': 20,
        'is_active': True,
    },
    {
        'code': 'gold_mouse',
        'name': '黄金鼠鼠',
        'min_consumption': Decimal('5000.00'),
        'benefits': ['黄金鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'gold',
        'sort_order': 30,
        'is_active': True,
    },
    {
        'code': 'platinum_mouse',
        'name': '铂金鼠鼠',
        'min_consumption': Decimal('10000.00'),
        'benefits': ['铂金鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'platinum',
        'sort_order': 40,
        'is_active': True,
    },
    {
        'code': 'emerald_mouse',
        'name': '翡翠鼠鼠',
        'min_consumption': Decimal('50000.00'),
        'benefits': ['翡翠鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'emerald',
        'sort_order': 50,
        'is_active': True,
    },
    {
        'code': 'diamond_mouse',
        'name': '钻石鼠鼠',
        'min_consumption': Decimal('100000.00'),
        'benefits': ['钻石鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'diamond',
        'sort_order': 60,
        'is_active': True,
    },
    {
        'code': 'glory_mouse',
        'name': '荣耀鼠鼠',
        'min_consumption': Decimal('200000.00'),
        'benefits': ['荣耀鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'glory',
        'sort_order': 70,
        'is_active': True,
    },
    {
        'code': 'brilliant_mouse',
        'name': '璀璨鼠鼠',
        'min_consumption': Decimal('350000.00'),
        'benefits': ['璀璨鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'brilliant',
        'sort_order': 80,
        'is_active': True,
    },
    {
        'code': 'dream_mouse',
        'name': '梦幻鼠鼠',
        'min_consumption': Decimal('500000.00'),
        'benefits': ['梦幻鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'dream',
        'sort_order': 90,
        'is_active': True,
    },
    {
        'code': 'elegant_mouse',
        'name': '绮丽鼠鼠',
        'min_consumption': Decimal('750000.00'),
        'benefits': ['绮丽鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'elegant',
        'sort_order': 100,
        'is_active': True,
    },
    {
        'code': 'supreme_mouse',
        'name': '至臻鼠鼠',
        'min_consumption': Decimal('1000000.00'),
        'benefits': ['至臻鼠鼠专属头衔', '累计钻石记录'],
        'badge_color': 'supreme',
        'sort_order': 110,
        'is_active': True,
    },
]


def seed_mouse_vip_tiers(apps, schema_editor):
    VipTier = apps.get_model('accounts', 'VipTier')
    ClientProfile = apps.get_model('accounts', 'ClientProfile')

    active_codes = []
    for payload in MOUSE_VIP_TIERS:
        active_codes.append(payload['code'])
        VipTier.objects.update_or_create(
            code=payload['code'],
            defaults=payload,
        )

    # 旧的普通会员、白银VIP、黄金VIP、黑金VIP保留历史记录，但不再参与等级匹配。
    VipTier.objects.exclude(code__in=active_codes).update(is_active=False)

    tiers = list(
        VipTier.objects
        .filter(code__in=active_codes, is_active=True)
        .order_by('-min_consumption', '-sort_order', '-id')
    )
    for profile in ClientProfile.objects.all().iterator():
        total = profile.cumulative_consumption or Decimal('0.00')
        matched = next((tier for tier in tiers if tier.min_consumption <= total), None)
        ClientProfile.objects.filter(pk=profile.pk).update(
            vip_tier_id=matched.pk if matched else None,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_p2_vip_consumption'),
    ]

    operations = [
        migrations.RunPython(seed_mouse_vip_tiers, migrations.RunPython.noop),
    ]
