# Generated for package image upload management

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0003_package_image_url_package_picture_url_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='PackageImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image_type', models.CharField(choices=[('cover', '封面图'), ('gallery', '轮播图'), ('detail', '详情长图')], default='gallery', max_length=20, verbose_name='图片类型')),
                ('image', models.ImageField(blank=True, null=True, upload_to='packages/%Y/%m/', verbose_name='上传图片')),
                ('external_url', models.CharField(blank=True, help_text='可选。已经在 CDN/OSS 的图片可填这里；上传图片和外链二选一即可。', max_length=500, null=True, verbose_name='外链图片URL')),
                ('sort_order', models.IntegerField(default=0, verbose_name='排序')),
                ('is_active', models.BooleanField(default=True, verbose_name='是否启用')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('package', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='images', to='catalog.package', verbose_name='所属商品')),
            ],
            options={
                'verbose_name': '商品图片',
                'verbose_name_plural': '商品图片列表',
                'db_table': 'package_images',
                'ordering': ['image_type', 'sort_order', 'id'],
            },
        ),
    ]
