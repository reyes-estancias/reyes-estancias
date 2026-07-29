from django.db import models
from django.utils.timezone import make_aware, now
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from core.tzutils import compose_aware_dt
from django.db import models, transaction
from django.db.models import Q
import secrets
import logging

logger = logging.getLogger(__name__)
# Create your models here.

LIMPIEZA = Decimal("100.00")
TAX_IMPUESTOS = Decimal("0.16")

class Property (models.Model):
    EMAIL_GROUP_EXPO_AGUAMARINA = "expo_aguamarina"
    EMAIL_GROUP_DEPARTAMENTOS = "departamentos"
    EMAIL_GROUP_OTROS = "otros"
    EMAIL_GROUP_CHOICES = [
        (EMAIL_GROUP_EXPO_AGUAMARINA, "Expo / Aguamarina"),
        (EMAIL_GROUP_DEPARTAMENTOS, "Departamentos"),
        (EMAIL_GROUP_OTROS, "Resto de casas (Santa Anita, Moras...)"),
    ]

    name = models.CharField(verbose_name= "Nombre", max_length=200)
    email_group = models.CharField(
        verbose_name="Grupo de email de confirmación",
        max_length=30,
        choices=EMAIL_GROUP_CHOICES,
        default=EMAIL_GROUP_OTROS,
        help_text="Determina qué instrucciones de llegada se envían al huésped.",
    )
    description = models.TextField(verbose_name="Descripción")
    tu_propiedad = models.TextField(verbose_name="Tu propiedad", blank=True, null=True)
    servicios_zonas_comunes = models.TextField(verbose_name="Servicios y zonas comunes", blank=True, null=True)
    interaccion_viajeros = models.TextField(verbose_name="Interacción con los viajeros", blank=True, null=True)
    otros_detalles = models.TextField(verbose_name="Otros detalles importantes", blank=True, null=True)
    beds = models.CharField(verbose_name= "Número de camas", max_length=200, default="Cama matrimonial")
    max_people = models.IntegerField(verbose_name="Capacidad")
    nightly_price = models.DecimalField(verbose_name="Precio por Noche", null=True, blank=True, decimal_places=2, max_digits=10)
    address = models.CharField(max_length= 200, verbose_name="Localización")
    latitude = models.FloatField(blank=True, null= True, verbose_name="Latitud")
    longitude = models.FloatField(blank=True, null= True, verbose_name="Altitud")
    #Importar calendarios desde Airbnb a esta web
    airbnb_ical_url = models.URLField("Calendario iCal de Airbnb", blank=True, null=True)
    #Exportar calendarios desde esta web a Airbnb 
    ical_token = models.CharField(max_length=100, blank=True, null=True, unique=True)
    #Cada vez que llame a save(ya sea desde el admin, desde scripts, views, forms...)se ejecutará la 
    # función automáticamente y se creará un "ical_token" para la nueva propiedad añadida.
    #SI no lo hiciese así y simplemente creara una función que hiciese lo mismo, tendría que llamarla 
    # yo manualmente cada vez que crease una nueva propiedad
    def save(self, *args, **kwargs):
        if not self.ical_token:
            self.ical_token = secrets.token_urlsafe(48)
        super().save(*args, **kwargs)


    def is_available(self, checkin, checkout, cant_personas, *, exclude_booking_id=None, buffer_nights=0):
        """
        Verifica si la propiedad está disponible para las fechas dadas.

        Realiza las siguientes verificaciones:
        1. Validación de fechas (formato, orden, no en el pasado)
        2. Validación de número de noches (2-365)
        3. Validación de capacidad
        4. Verificación contra calendarios externos (Airbnb, Booking.com)
        5. Verificación contra reservas locales confirmadas/pendientes

        Args:
            checkin: Fecha de check-in (str, date, o datetime)
            checkout: Fecha de check-out (str, date, o datetime)
            cant_personas: Número de personas
            exclude_booking_id: ID de reserva a excluir de la verificación
            buffer_nights: Noches de buffer a agregar antes/después

        Returns:
            bool: True si está disponible, False si no

        Note:
            Por seguridad, si falla la verificación del calendario externo,
            la propiedad se considera NO disponible (fail-safe).
        """
        # 1. Parsear y validar fechas
        try:
            checkin_dt = compose_aware_dt(checkin, hour=15, minute=0)
            checkout_dt = compose_aware_dt(checkout, hour=12, minute=0)
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning(
                f"Error parseando fechas en is_available: checkin={checkin}, checkout={checkout}, error={e}",
                extra={'property_id': self.id}
            )
            return False

        if not checkin_dt or not checkout_dt:
            logger.warning(f"Fechas nulas en is_available para propiedad {self.id}")
            return False

        # 2. Validar que checkout sea después de checkin
        if checkout_dt <= checkin_dt:
            logger.debug(f"Checkout debe ser después de checkin: {checkin_dt} >= {checkout_dt}")
            return False

        # 3. Validar que no sea en el pasado
        current_time = now()
        if checkin_dt < current_time:
            logger.debug(f"Check-in en el pasado: {checkin_dt} < {current_time}")
            return False

        # 4. Validar número de noches (mínimo 2, máximo 365)
        nights = (checkout_dt.date() - checkin_dt.date()).days
        if nights < 2:
            logger.debug(f"Estancia demasiado corta: {nights} noche(s), mínimo 2")
            return False
        if nights > 365:
            logger.debug(f"Estancia demasiado larga: {nights} días, máximo 365")
            return False

        # 5. Validar capacidad
        try:
            cant_personas_int = int(cant_personas)
        except (ValueError, TypeError):
            logger.warning(f"Número de personas inválido: {cant_personas}")
            return False

        if self.max_people < cant_personas_int:
            logger.debug(f"Excede capacidad: {cant_personas_int} personas, máximo {self.max_people}")
            return False

        # 6. Aplicar buffer si es necesario
        if buffer_nights:
            checkin_dt -= timedelta(days=buffer_nights)
            checkout_dt += timedelta(days=buffer_nights)

        # 7. Verificar conflictos con calendarios externos (Airbnb, Booking.com, etc.)
        if self.airbnb_ical_url:
            try:
                from properties.utils.ical import fetch_ical_bookings
                blocked_ranges = fetch_ical_bookings(self.airbnb_ical_url)
                checkin_date = checkin_dt.date()
                checkout_date = checkout_dt.date()

                for start, end in blocked_ranges:
                    # Verificar solapamiento: dos rangos se solapan si start1 < end2 AND start2 < end1
                    if start < checkout_date and end > checkin_date:
                        logger.info(
                            f"Reserva rechazada por calendario externo para propiedad {self.id} '{self.name}': "
                            f"rango bloqueado [{start}, {end}] solapa con solicitud [{checkin_date}, {checkout_date}]"
                        )
                        return False
            except ValueError as e:
                # Error de validación (host no permitido, timeout, etc.)
                logger.warning(
                    f"Error de validación al verificar calendario externo para propiedad {self.id}: {e}",
                    extra={'property_id': self.id, 'ical_url': self.airbnb_ical_url[:100]}
                )
                # Por seguridad, si falla la validación, NO permitir la reserva
                return False
            except Exception as e:
                # Error inesperado
                logger.error(
                    f"Error inesperado al verificar calendario externo para propiedad {self.id}: {e}",
                    exc_info=True,
                    extra={'property_id': self.id, 'ical_url': self.airbnb_ical_url[:100]}
                )
                # Por seguridad, si falla el fetch, NO permitir la reserva
                return False

        # 8. Verificar conflictos con reservas existentes
        # Solo consideramos reservas "confirmed" o "pending" (no "expired" ni "cancelled")
        qs = self.bookings.filter(status__in=["confirmed", "pending"])
        # Excluir reservas pendientes con hold expirado
        qs = qs.exclude(status="pending", hold_expires_at__lt=current_time)
        # Excluir reservas confirmadas que ya pasaron (doble seguridad)
        qs = qs.exclude(status="confirmed", departure__lt=current_time)

        if exclude_booking_id:
            qs = qs.exclude(id=exclude_booking_id)

        for booking in qs:
            if (booking.arrival < checkout_dt) and (booking.departure > checkin_dt):
                logger.debug(
                    f"Conflicto con reserva {booking.id}: "
                    f"[{booking.arrival}, {booking.departure}] solapa con [{checkin_dt}, {checkout_dt}]"
                )
                return False

        return True
    
    def _to_date(self, value):
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            value = datetime.strptime(value, "%Y-%m-%d").date()
            return value
        raise ValueError("La fecha seleccionada no es válida")
    
    def quote_total(self, checkin, checkout):
        checkin = self._to_date(checkin)
        checkout = self._to_date(checkout)

        if not checkin or not checkout:
            raise ValueError("Fechas incompletas")
        
        days = (checkout - checkin).days

        if days <= 0:
            raise ValueError("Fechas mal configuradas")

        nightly = self.nightly_price
        if not nightly:
            raise ValueError("Faltan tarifas por configurar")
        

        
        info = {}
        subtotal_base = (nightly * days).quantize(Decimal("0.01"))

        if days >= 30:
            discount_rate = Decimal("0.20")
        
        elif days >= 7:
            discount_rate = Decimal("0.10")
        
        else:
            discount_rate = Decimal("0.00")

        discount_amount = (subtotal_base * discount_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        subtotal = (subtotal_base - discount_amount).quantize(Decimal("0.01"))

        taxable = (subtotal + LIMPIEZA).quantize(Decimal("0.01"))
        tax_amount = (taxable * TAX_IMPUESTOS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total = (taxable + tax_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        info["days"] = days
        info["nightly"] = nightly
        info["subtotal_base"]= subtotal_base
        info["discount_rate"]= discount_rate
        info["discount_amount"]= discount_amount
        info["subtotal"]= subtotal
        info["cleaning"]= LIMPIEZA
        info["taxable"]= taxable
        info["tax_amount"]= tax_amount
        info["total"]= total
        
        return info
    
    def get_blocked_ranges(self):
        """
            Devuelve una lista de tuplas (start_date, end_date) con las fechas bloqueadas
            según el calendario iCal de Airbnb.
        """
        from properties.utils.ical import get_blocked_dates

        if not self.airbnb_ical_url:
            return []
        
        try:
            return get_blocked_dates(self.airbnb_ical_url)
        except ValueError as e:
            # Error de validación (host no permitido, timeout, etc.)
            logger.warning(
                f"Error de validación al obtener bloqueos iCal para propiedad '{self.name}' (ID: {self.id}): {e}",
                extra={'property_id': self.id, 'ical_url': self.airbnb_ical_url[:100]}
            )
            return []
        except Exception as e:
            # Error inesperado
            logger.error(
                f"Error inesperado al obtener bloqueos iCal para propiedad '{self.name}' (ID: {self.id}): {e}",
                exc_info=True,
                extra={'property_id': self.id, 'ical_url': self.airbnb_ical_url[:100]}
            )
            return []

        

    

    class Meta():
        verbose_name = "Propiedad"
        verbose_name_plural = "Propiedades"

    def __str__(self):
        return self.name

class PropertyImage(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="properties", verbose_name="Imagen")
    cover = models.BooleanField(default=False, db_index=True, verbose_name="Portada")
    position = models.PositiveIntegerField(default=0, help_text="Orden en la galería")

    class Meta:
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["property", "cover"])]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.cover:
            with transaction.atomic():
                PropertyImage.objects.filter(property_id=self.property_id, cover=True)\
                                     .exclude(pk=self.pk).update(cover=False)