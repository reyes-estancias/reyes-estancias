from django.contrib.auth.models import AbstractUser
from django.db import models
from django.core.validators import RegexValidator

phone_validator = RegexValidator(
    regex=r'^\+?[0-9]{7,15}$',
    message="Teléfono inválido (7–15 dígitos, + opcional)."
)

class User(AbstractUser):
    username = models.CharField(max_length=150, unique=True, blank=True, verbose_name="Usuario")
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, validators=[phone_validator], blank=True)
    email_verified = models.BooleanField(default=False, verbose_name="Email verificado")

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    def save(self, *args, **kwargs):
        if not self.username:
            self.username = self.email[:150]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email