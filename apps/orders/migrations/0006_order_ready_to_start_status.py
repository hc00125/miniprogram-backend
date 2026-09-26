from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0005_order_kook_room'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('待接单', '待接单'),
                    ('待支付', '待支付'),
                    ('待开打', '待开打'),
                    ('进行中', '进行中'),
                    ('已完成', '已完成'),
                    ('已取消', '已取消'),
                ],
                default='待接单',
                max_length=20,
            ),
        ),
    ]
