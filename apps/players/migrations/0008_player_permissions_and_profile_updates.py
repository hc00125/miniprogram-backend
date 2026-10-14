import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0007_alter_player_options_alter_playerapplication_options'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='can_accept_orders',
            field=models.BooleanField(default=True, verbose_name='允许接单'),
        ),
        migrations.AddField(
            model_name='player',
            name='can_be_designated',
            field=models.BooleanField(default=True, verbose_name='允许被指定'),
        ),
        migrations.AddField(
            model_name='player',
            name='can_withdraw',
            field=models.BooleanField(default=True, verbose_name='允许提现'),
        ),
        migrations.AddField(
            model_name='player',
            name='is_publicly_visible',
            field=models.BooleanField(default=True, verbose_name='在陪玩列表展示'),
        ),
        migrations.CreateModel(
            name='PlayerProfileUpdateRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bio', models.TextField(blank=True, default='', verbose_name='新个人简介')),
                ('audio_intro_url', models.CharField(blank=True, default='', max_length=500, verbose_name='新音频URL')),
                ('audio_intro_title', models.CharField(blank=True, default='', max_length=100, verbose_name='新音频标题')),
                ('status', models.CharField(choices=[('pending', '待审核'), ('approved', '已通过'), ('rejected', '已拒绝'), ('cancelled', '已取消')], db_index=True, default='pending', max_length=20)),
                ('reject_reason', models.CharField(blank=True, default='', max_length=300)),
                ('submitted_at', models.DateTimeField(auto_now_add=True)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='profile_update_requests', to='players.player', verbose_name='陪玩师')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_player_profile_updates', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': '陪玩资料修改申请',
                'verbose_name_plural': '陪玩资料修改申请',
                'db_table': 'player_profile_update_requests',
                'ordering': ['-submitted_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='playerprofileupdaterequest',
            constraint=models.UniqueConstraint(condition=Q(status='pending'), fields=('player',), name='uniq_pending_player_profile_update'),
        ),
    ]
