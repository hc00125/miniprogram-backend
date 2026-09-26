from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('orders', '0026_order_created_by_order_customer_order_source')]
    operations = [
        # Django's Python default does not cover inserts made by an older running
        # release. Keep a DB default so a code-only rollback remains safe.
        # It changes no existing rows and is removed automatically with the column.
        migrations.RunSQL("ALTER TABLE orders ALTER COLUMN source SET DEFAULT 'self'", migrations.RunSQL.noop),
    ]
