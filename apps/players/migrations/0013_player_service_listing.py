from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0009_package_owner_player_package_selling_mode_and_more'),
        ('players', '0012_playerordernoticesubscription'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PlayerServiceListing',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('pending', '待审核'), ('approved', '已上架'), ('rejected', '已拒绝'), ('offline', '已下架')], db_index=True, default='pending', max_length=20)),
                ('is_available', models.BooleanField(db_index=True, default=True, verbose_name='当前可预约')),
                ('custom_description', models.CharField(blank=True, default='', max_length=300, verbose_name='个人服务说明')),
                ('sort_order', models.IntegerField(default=0, verbose_name='个人排序')),
                ('rejection_reason', models.CharField(blank=True, default='', max_length=300, verbose_name='拒绝原因')),
                ('reviewed_at', models.DateTimeField(blank=True, null=True, verbose_name='审核时间')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='service_listings', to='players.player', verbose_name='陪玩师')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_player_service_listings', to=settings.AUTH_USER_MODEL, verbose_name='审核人')),
                ('spec', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='player_service_listings', to='catalog.packagespec', verbose_name='共享服务规格')),
            ],
            options={
                'verbose_name': '陪玩共享服务上架',
                'verbose_name_plural': '陪玩共享服务上架',
                'db_table': 'player_service_listings',
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='playerservicelisting',
            constraint=models.UniqueConstraint(fields=('player', 'spec'), name='uniq_player_shared_service_spec'),
        ),
        migrations.AddIndex(
            model_name='playerservicelisting',
            index=models.Index(fields=['player', 'status', 'is_available'], name='player_listing_public_idx'),
        ),
    ]
