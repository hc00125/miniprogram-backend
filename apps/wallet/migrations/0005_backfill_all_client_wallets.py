from django.db import migrations


def create_missing_client_wallets(apps, schema_editor):
    ClientProfile = apps.get_model('accounts', 'ClientProfile')
    ClientWallet = apps.get_model('wallet', 'ClientWallet')

    using = schema_editor.connection.alias
    existing_profile_ids = ClientWallet.objects.using(using).values_list('profile_id', flat=True)
    missing_profile_ids = (
        ClientProfile.objects.using(using)
        .exclude(pk__in=existing_profile_ids)
        .values_list('pk', flat=True)
        .iterator(chunk_size=1000)
    )

    batch = []
    for profile_id in missing_profile_ids:
        batch.append(ClientWallet(profile_id=profile_id))
        if len(batch) >= 1000:
            ClientWallet.objects.using(using).bulk_create(batch, ignore_conflicts=True)
            batch.clear()

    if batch:
        ClientWallet.objects.using(using).bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0004_diamond_admin_labels'),
    ]

    operations = [
        migrations.RunPython(create_missing_client_wallets, migrations.RunPython.noop),
    ]
