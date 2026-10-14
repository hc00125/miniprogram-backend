from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='Customer', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('nickname', models.CharField('老板称呼', max_length=100)),
        ('note', models.CharField('客服内部备注', max_length=300, blank=True, default='')),
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('user', models.OneToOneField(to=settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='dispatch_customer')),
        ('created_by', models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT, related_name='created_dispatch_customers')),
    ], options={'verbose_name':'客户档案','verbose_name_plural':'客户档案','permissions':[('use_console','使用客服派单工作台')]})]
