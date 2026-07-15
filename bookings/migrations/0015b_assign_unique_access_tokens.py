import uuid
from django.db import migrations


def assign_unique_access_tokens(apps, schema_editor):
    Booking = apps.get_model('bookings', 'Booking')
    for booking in Booking.objects.all():
        booking.access_token = uuid.uuid4()
        booking.save(update_fields=['access_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0015_populate_guest_fields'),
    ]

    operations = [
        migrations.RunPython(assign_unique_access_tokens, migrations.RunPython.noop),
    ]
