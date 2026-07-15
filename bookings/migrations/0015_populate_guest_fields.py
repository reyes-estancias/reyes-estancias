from django.db import migrations


def populate_guest_fields(apps, schema_editor):
    Booking = apps.get_model('bookings', 'Booking')
    for booking in Booking.objects.select_related('user').filter(user__isnull=False):
        user = booking.user
        full_name = f"{user.first_name} {user.last_name}".strip()
        booking.guest_name  = full_name or user.username
        booking.guest_email = user.email
        booking.guest_phone = getattr(user, 'phone', '') or ''
        booking.save(update_fields=['guest_name', 'guest_email', 'guest_phone'])


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0014_booking_guest_fields_and_magiclink'),
    ]

    operations = [
        migrations.RunPython(populate_guest_fields, migrations.RunPython.noop),
    ]
