from celery import shared_task
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from .models import Booking, MagicLink
from payments.services import compute_balance_due_snapshot
import logging

logger = logging.getLogger(__name__)


@shared_task
def mark_expired_bookings():
    """
    Marca como 'expired' todas las reservas confirmadas cuya fecha de checkout ya pasó.

    Se ejecuta periódicamente via Celery Beat (configurado para ejecutarse diariamente).

    Returns:
        str: Resumen de reservas actualizadas
    """
    now = timezone.now()

    # Buscar reservas confirmadas que ya pasaron su fecha de checkout
    # Las reservas de Airbnb las gestiona el sync de iCal, no este task
    expired_bookings = Booking.objects.filter(
        status="confirmed",
        departure__lt=now,
    ).exclude(source="airbnb")

    count = expired_bookings.count()

    if count > 0:
        # Actualizar todas a expired
        updated = expired_bookings.update(status="expired")

        logger.info(
            f"Marcadas {updated} reservas como expiradas. "
            f"Fecha de corte: {now.isoformat()}"
        )

        return f"expired={updated}"
    else:
        logger.debug("No hay reservas para marcar como expiradas")
        return "expired=0"


@shared_task
def mark_completed_bookings():
    """
    Marca como 'completed' las reservas confirmadas cuya fecha de salida ya pasó
    y cuyo balance está completamente pagado (compute_balance_due_snapshot == 0).

    Se ejecuta periódicamente via Celery Beat.
    """
    now = timezone.now()

    # Las reservas de Airbnb tienen total_amount=0 y se marcarían siempre como completed
    candidates = Booking.objects.filter(
        status="confirmed",
        departure__lt=now,
    ).exclude(source="airbnb")

    completed = 0
    for booking in candidates.iterator():
        if compute_balance_due_snapshot(booking) == 0:
            booking.status = "completed"
            booking.save(update_fields=["status"])
            completed += 1

    if completed:
        logger.info("Marcadas %d reservas como completed.", completed)
    else:
        logger.debug("No hay reservas para marcar como completed.")

    return f"completed={completed}"


@shared_task
def mark_expired_holds():
    """
    Marca como 'expired' las reservas pendientes cuyo hold_expires_at ya pasó.

    Esto es para reservas que se crearon pero nunca se pagó el depósito.

    Returns:
        str: Resumen de reservas expiradas
    """
    now = timezone.now()

    # Buscar reservas pendientes con hold expirado
    expired_holds = Booking.objects.filter(
        status="pending",
        hold_expires_at__isnull=False,
        hold_expires_at__lt=now
    )

    count = expired_holds.count()

    if count > 0:
        updated = expired_holds.update(status="expired")

        logger.info(
            f"Marcadas {updated} reservas pendientes como expiradas por hold vencido. "
            f"Fecha de corte: {now.isoformat()}"
        )

        return f"holds_expired={updated}"
    else:
        logger.debug("No hay holds expirados para marcar")
        return "holds_expired=0"


@shared_task
def purge_magic_links():
    """
    Limpia registros MagicLink obsoletos:
      - Expirados (expires_at < now), usados o no.
      - Usados hace más de 24 h (margen de depuración para soporte).
    """
    now = timezone.now()
    deleted, _ = MagicLink.objects.filter(
        Q(expires_at__lt=now) |
        Q(used=True, created_at__lt=now - timedelta(hours=24))
    ).delete()

    if deleted:
        logger.info("purge_magic_links: eliminados %d registros.", deleted)
    else:
        logger.debug("purge_magic_links: nada que eliminar.")

    return f"deleted={deleted}"
