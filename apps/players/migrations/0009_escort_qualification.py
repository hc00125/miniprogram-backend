from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('players', '0008_player_permissions_and_profile_updates'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlayerEscortQualification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('none', '未申请'), ('pending', '审核中'), ('approved', '已通过'), ('rejected', '未通过'), ('suspended', '已暂停')], db_index=True, default='none', max_length=20, verbose_name='护航资格状态')),
                ('review_note', models.CharField(blank=True, default='', max_length=300, verbose_name='审核备注')),
                ('reviewed_at', models.DateTimeField(blank=True, null=True, verbose_name='审核时间')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('player', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='escort_qualification', to='players.player', verbose_name='陪玩师')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_player_escort_qualifications', to=settings.AUTH_USER_MODEL, verbose_name='审核人')),
            ],
            options={
                'verbose_name': '护航资格',
                'verbose_name_plural': '护航资格',
                'db_table': 'player_escort_qualifications',
            },
        ),
        migrations.CreateModel(
            name='PlayerEscortApplication',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('experience', models.TextField(verbose_name='护航经历与能力说明')),
                ('evidence_urls', models.JSONField(blank=True, default=list, verbose_name='证明材料链接')),
                ('status', models.CharField(choices=[('pending', '待审核'), ('approved', '已通过'), ('rejected', '未通过')], db_index=True, default='pending', max_length=20)),
                ('reject_reason', models.CharField(blank=True, default='', max_length=300, verbose_name='未通过原因')),
                ('review_note', models.CharField(blank=True, default='', max_length=300, verbose_name='审核备注')),
                ('submitted_at', models.DateTimeField(auto_now_add=True)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='escort_applications', to='players.player', verbose_name='陪玩师')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_escort_applications', to=settings.AUTH_USER_MODEL, verbose_name='审核人')),
            ],
            options={
                'verbose_name': '护航资格申请',
                'verbose_name_plural': '护航资格申请',
                'db_table': 'player_escort_applications',
                'ordering': ['-submitted_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='playerescortapplication',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'pending')), fields=('player',), name='uniq_pending_player_escort_application'),
        ),
    ]
