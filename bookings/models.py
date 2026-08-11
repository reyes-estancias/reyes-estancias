from django.db import models
from django.contrib.auth.models import User
from properties.models import Property
from decimal import Decimal
from django.db.models import Sum
from reyes_estancias import settings
import uuid
from django.utils import timezone
from datetime import timedelta

# Create your models here.

class Booking(models.Model):
    STATUS_CHOICES = [
    ("pending", "Pendiente"),
    ("confirmed", "Confirmado" ),
    ("cancelled", "Cancelado"),
    ("expired", "Expirada"),
    ("completed", "Completada"),
    ]
    SOURCE_CHOICES = [
        ("web", "Web"),
        ("airbnb", "Airbnb"),
    ]
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="web", verbose_name="Origen")
    ical_uid = models.CharField(max_length=500, blank=True, default="", db_index=True, verbose_name="UID iCal")
    airbnb_confirmation_code = models.CharField(max_length=50, blank=True, default="", verbose_name="Código confirmación Airbnb")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Usuario")
    guest_name  = models.CharField(max_length=200, blank=True, default="", verbose_name="Nombre huésped")
    guest_email = models.EmailField(blank=True, default="", verbose_name="Email huésped")
    guest_phone = models.CharField(max_length=20, blank=True, default="", verbose_name="Teléfono huésped")
    access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name="Token de acceso")
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="bookings")
    person_num = models.IntegerField(verbose_name="Cant.Personas")
    arrival = models.DateTimeField(verbose_name="LLegada")
    departure = models.DateTimeField(verbose_name="Salida")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2,default=Decimal("0.00"), verbose_name="Monto total")
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"), verbose_name="Total Depósito")
    balance_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"), verbose_name="Total Balance")
    status = models.CharField(max_length=20,choices=STATUS_CHOICES, default="pending", verbose_name="Estado")
    hold_expires_at = models.DateTimeField(null=True, blank=True, verbose_name="Expira")

    #Campos necesarios para el segundo cobro
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="Id cliente stripe")
    stripe_payment_method_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="Método de pago")
    
    #ETA PARA COBRO OFF-SESSION CON CELERY 
    balance_charge_task_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="Identificador de la tarea")
    balance_charge_eta = models.DateTimeField(null=True, blank=True, verbose_name="Fecha para cobro automático de balance")
    
    def deposit_payment(self):
        return self.payments.filter(payment_type="deposit").order_by("-id").first()

    def deposit_paid(self):
        p = self.deposit_payment()
        return bool(p and p.status == "paid")
    
    
    def balance_payment(self):
        return self.payments.filter(payment_type="balance").order_by("-id").first()
    
    def balance_paid(self):
        p = self.balance_payment()
        return bool(p and p.status == "paid")
    
    def dep_before_chage_dates(self):
        return self.payments.filter(payment_type="deposit", status="paid").aggregate(s=Sum("amount")) ["s"] or Decimal("0.00")
    
    def net_deposit_paid(self):
        paid_dep = self.payments.filter(payment_type="deposit", status="paid").aggregate(s=Sum("amount")) ["s"] or Decimal("0.00")
        refunded = self.payments.filter(payment_type="deposit", status="paid").aggregate(s=Sum("refunded_amount")) ["s"] or Decimal("0.00")
        return (paid_dep - refunded)


    class Meta():
        verbose_name = "Reserva"
        verbose_name_plural = "Reservas"
        indexes = [
            models.Index(fields=['property', 'status', 'arrival', 'departure'], name='booking_avail_idx'),
            models.Index(fields=['guest_email', 'status'], name='booking_guest_email_idx'),
            models.Index(fields=['status', 'hold_expires_at'], name='booking_hold_idx'),
            models.Index(fields=['arrival', 'departure'], name='booking_dates_idx'),
        ]

    def __str__(self):
        guest = self.guest_name or (self.user.username if self.user else "Sin huésped")
        return f"{self.property.name} - {guest} - ({self.arrival} => {self.departure})"
    

MAGIC_LINK_TTL_MINUTES = 20

class MagicLink(models.Model):
    email      = models.EmailField(verbose_name="Email")
    token      = models.UUIDField(default=uuid.uuid4, unique=True, verbose_name="Token")
    expires_at = models.DateTimeField(verbose_name="Expira")
    used       = models.BooleanField(default=False, verbose_name="Usado")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Creado")

    class Meta:
        verbose_name = "Magic Link"
        verbose_name_plural = "Magic Links"
        indexes = [
            models.Index(fields=['token'], name='magiclink_token_idx'),
            models.Index(fields=['email', 'used'], name='magiclink_email_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
        super().save(*args, **kwargs)

    def is_valid(self):
        return not self.used and timezone.now() < self.expires_at

    def __str__(self):
        return f"{self.email} — {'usado' if self.used else 'activo'}"


class BookingChangeLog(models.Model):
    LOG_STATUS_CHOICES = [
        ("pending", "Pendiente"),
        ("applied", "Aplicado"),
        ("superseded", "Reemplazado"),
    ]

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="change_logs", verbose_name="Reserva modificada")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, verbose_name="Usuario", null=True, blank=True)
    
    old_arrival = models.DateTimeField(verbose_name="Llegada antigua")
    old_departure = models.DateTimeField(verbose_name="Salida antigua")
    new_arrival = models.DateTimeField(verbose_name="Llegada nueva")
    new_departure = models.DateTimeField(verbose_name="Salida nueva")
    
    old_T = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Antiguo total")
    new_T = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Nuevo total")
    
    paid_dep = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Depósito pagado", default=Decimal("0.00"))
    deposit_topup = models.DecimalField(max_digits=10, decimal_places=2,default=Decimal("0.00"), verbose_name="Depósito extra")
    deposit_target = models.DecimalField(max_digits=10, decimal_places=2,default=Decimal("0.00"), verbose_name="Nuevo depósito")
    deposit_refund = models.DecimalField(max_digits=10, decimal_places=2,default=Decimal("0.00"), verbose_name="Devolución")
    
    old_balance = models.DecimalField(max_digits=10, decimal_places=2,default=Decimal("0.00"), verbose_name="Balance antiguo")
    new_balance_due = models.DecimalField(max_digits=10, decimal_places=2,default=Decimal("0.00"), verbose_name="Nuevo balance")
    
    status = models.CharField(max_length=60, choices=LOG_STATUS_CHOICES, verbose_name="Estado", default="pending")
    topup_payment = models.ForeignKey("payments.Payment", on_delete=models.SET_NULL, blank=True, null=True, related_name="change_logs", verbose_name="Pago top up ")
    checkout_session_id = models.CharField(max_length=255, null=True, blank=True, verbose_name="Checkout ID (Stripe)")
    superseded_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Creado")

    class Meta:
        verbose_name="Registro de cambio de reservas"
        verbose_name_plural="Registros de cambio de reservas"

    def __str__(self):
        return f"{self.booking.property.name} - {self.actor} - ({self.new_arrival} => {self.new_departure})"