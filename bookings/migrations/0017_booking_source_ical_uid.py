from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0016_booking_access_token_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='source',
            field=models.CharField(
                choices=[('web', 'Web'), ('airbnb', 'Airbnb')],
                default='web',
                max_length=20,
                verbose_name='Origen',
            ),
        ),
        migrations.AddField(
            model_name='booking',
            name='ical_uid',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                max_length=500,
                verbose_name='UID iCal',
            ),
        ),
    ]
