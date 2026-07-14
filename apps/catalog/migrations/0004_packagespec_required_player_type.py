from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


def backfill_required_player_types(apps, schema_editor):
    PackageSpec = apps.get_model('catalog', 'PackageSpec')
    PlayerType = apps.get_model('catalog', 'PlayerType')

    for keyword in ('娱乐', '技术', '金牌', '明星'):
        player_type = PlayerType.objects.filter(name__icontains=keyword).order_by('priority', 'id').first()
        if not player_type:
            continue
        PackageSpec.objects.filter(required_player_type__isnull=True).filter(
            Q(name__icontains=keyword)
            | Q(short_name__icontains=keyword)
            | Q(display_name__icontains=keyword)
        ).update(required_player_type=player_type)


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0003_package_image_url_package_picture_url_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='packagespec',
            name='required_player_type',
            field=models.ForeignKey(
                blank=True,
                help_text='指定具体陪玩时必须与该类型完全一致；留空表示该规格不限制陪玩类型。',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='required_package_specs',
                to='catalog.playertype',
                verbose_name='指定陪玩类型',
            ),
        ),
        migrations.RunPython(backfill_required_player_types, migrations.RunPython.noop),
    ]
