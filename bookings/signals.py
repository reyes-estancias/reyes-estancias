from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Booking
from payments.models import Payment

@receiver(post_save, sender=Booking)
def create_payment_for_booking(sender, instance, created, **kwargs):
    if created and instance.source == "web":
        Payment.objects.create(
            booking=instance,
            status="pending",
            amount=0.00,
            currency="MXN",
        )