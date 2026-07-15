import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.utils.timezone
from datetime import timedelta


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0013_add_completed_status'),
        (settings.AUTH_USER_MODEL.split('.')[0], '0002_user_email_verified'),
    ]

    operations = [
        # 1. user: CASCADE → SET_NULL, nullable
        migrations.AlterField(
            model_name='booking',
            name='user',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
                verbose_name='Usuario',
            ),
        ),

        # 2. Campos de huésped (sin unique, con default vacío para filas existentes)
        migrations.AddField(
            model_name='booking',
            name='guest_name',
            field=models.CharField(blank=True, default='', max_length=200, verbose_name='Nombre huésped'),
        ),
        migrations.AddField(
            model_name='booking',
            name='guest_email',
            field=models.EmailField(blank=True, default='', verbose_name='Email huésped'),
        ),
        migrations.AddField(
            model_name='booking',
            name='guest_phone',
            field=models.CharField(blank=True, default='', max_length=20, verbose_name='Teléfono huésped'),
        ),

        # 3. access_token sin unique todavía (se añade en 0016 tras poblar valores)
        migrations.AddField(
            model_name='booking',
            name='access_token',
            field=models.UUIDField(default=uuid.uuid4, editable=False, verbose_name='Token de acceso'),
        ),

        # 4. Reemplazar booking_user_idx por guest_email_idx
        migrations.RemoveIndex(
            model_name='booking',
            name='booking_user_idx',
        ),
        migrations.AddIndex(
            model_name='booking',
            index=models.Index(fields=['guest_email', 'status'], name='booking_guest_email_idx'),
        ),

        # 5. Modelo MagicLink
        migrations.CreateModel(
            name='MagicLink',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('email', models.EmailField(verbose_name='Email')),
                ('token', models.UUIDField(default=uuid.uuid4, unique=True, verbose_name='Token')),
                ('expires_at', models.DateTimeField(verbose_name='Expira')),
                ('used', models.BooleanField(default=False, verbose_name='Usado')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Creado')),
            ],
            options={
                'verbose_name': 'Magic Link',
                'verbose_name_plural': 'Magic Links',
            },
        ),
        migrations.AddIndex(
            model_name='magiclink',
            index=models.Index(fields=['token'], name='magiclink_token_idx'),
        ),
        migrations.AddIndex(
            model_name='magiclink',
            index=models.Index(fields=['email', 'used'], name='magiclink_email_idx'),
        ),
    ]
