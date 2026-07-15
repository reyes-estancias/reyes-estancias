from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0015b_assign_unique_access_tokens'),
    ]

    operations = [
        migrations.AlterField(
            model_name='booking',
            name='access_token',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='Token de acceso'),
        ),
    ]
