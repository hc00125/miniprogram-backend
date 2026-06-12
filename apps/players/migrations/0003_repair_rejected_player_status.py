from django.db import migrations
from django.utils import timezone


def repair_rejected_player_status(apps, schema_editor):
    PlayerApplication = apps.get_model('players', 'PlayerApplication')
    ClientProfile = apps.get_model('accounts', 'ClientProfile')

    # 只以每个用户最新的一条申请为准，避免旧的拒绝记录覆盖后续已通过状态。
    latest_status_by_user = {}
    applications = (
        PlayerApplication.objects
        .order_by('user_id', '-submitted_at', '-id')
        .values_list('user_id', 'status')
    )
    for user_id, application_status in applications.iterator():
        if user_id not in latest_status_by_user:
            latest_status_by_user[user_id] = application_status

    rejected_user_ids = [
        user_id
        for user_id, application_status in latest_status_by_user.items()
        if application_status == 'rejected'
    ]
    if not rejected_user_ids:
        return

    # 仅修复受旧后台 Bug 影响、仍停留在 pending 的资料，避免覆盖其他合法状态。
    ClientProfile.objects.filter(
        user_id__in=rejected_user_ids,
        player_status='pending',
    ).update(
        player_status='rejected',
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('players', '0002_playerapplication_player_type'),
    ]

    operations = [
        migrations.RunPython(
            repair_rejected_player_status,
            migrations.RunPython.noop,
        ),
    ]
