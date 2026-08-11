from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db import transaction
from bookings.models import Booking
from payments.models import Payment
from .services import charge_offsession_with_fallback, compute_balance_due_snapshot
from django.db.models import Exists, OuterRef
import logging

logger = logging.getLogger(__name__)



@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def charge_balance_for_booking(self, booking_id, base_url):

    """
    Cobra el balance de UNA reserva (idempotente).
    Reintenta si hay fallos transitorios.

    Args:
        booking_id: ID de la reserva
        base_url: URL base del sitio (ej: "https://tu-dominio.com")
    """

    try:
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=booking_id)
            if b.status != "confirmed":
                logger.info(f"Booking {booking_id} no está confirmado, omitiendo cobro")
                return "booking_not_confirmed"

            #info mínima
            if not (b.stripe_customer_id and b.stripe_payment_method_id):
                logger.warning(f"Booking {booking_id} no tiene método de pago guardado")
                return "missing_method"

            # no cobres si ya existe un balance pagado (guard contra race conditions y re-encolados)
            if Payment.objects.filter(booking=b, payment_type="balance", status="paid").exists():
                logger.info(f"Booking {booking_id} ya tiene balance pagado, omitiendo cobro")
                return "already_paid"

            # no cobres si no hace falta
            amount = compute_balance_due_snapshot(b)
            if amount <= 0:
                logger.info(f"Booking {booking_id} no tiene balance pendiente")
                return "no_balance"

            # no cobres si hay top-up pendiente que bloquee
            if Payment.objects.filter(
                booking=b,
                payment_type="deposit",
                metadata__payment_role="deposit_topup",
                status__in=["pending", "requires_action"]).exists():
                logger.info(f"Booking {booking_id} tiene top-up pendiente, omitiendo cobro de balance")
                return "pending_topup"


        # fuera del select_for_update para no bloquear durante Stripe

        logger.info(f"Iniciando cobro de balance para booking {booking_id}, monto: ${amount}")
        result = charge_offsession_with_fallback(
            booking=b,
            request=None,
            amount=amount,
            payment_type="balance",
            description="Cargo del 70%",
            base_url=base_url)
        status = result.get("status") if isinstance(result, dict) else result
        if status in ("paid", "already_paid"):
            logger.info(f"Cobro de balance exitoso para booking {booking_id}")
            return "succeeded"
        if status == "requires_action":
            logger.warning(f"Cobro de balance requiere acción del usuario para booking {booking_id}")
            return "requires_action"
        if status in ("no_balance", "skipped"):
            logger.info(f"No hay balance que cobrar para booking {booking_id}")
            return "no_balance"
        if status == "missing_method":
            logger.warning(f"Falta método de pago para booking {booking_id}")
            return "missing_method"

        logger.error(f"Cobro de balance falló para booking {booking_id}, status: {status}")
        return "failed"


    except Booking.DoesNotExist:
        logger.error(f"Booking {booking_id} no encontrado")
        return "not_found"
    except Exception as exc:
        # reintento básico (red/Stripe)
        logger.error(f"Error al cobrar balance para booking {booking_id}: {exc}")
        raise self.retry(exc=exc)

@shared_task
def scan_and_charge_balances(base_url):

    """
    Encola cobros para reservas cuyo check-in fue hace ≥ 24h.
    Usa Celery Beat para llamar a esta task (p.ej., cada 15 min).
    """

    cutoff = timezone.now() - timedelta(hours=48)

    # Excluir reservas cuyo balance YA está pagado. Debe ser una única subconsulta
    # (mismo Payment cumpliendo type=balance Y status=paid); con .exclude(a=.., b=..)
    # Django genera dos EXISTS separados y excluiría reservas con depósito pagado +
    # balance pendiente (que sí hay que cobrar).
    paid_balance = Payment.objects.filter(
        booking=OuterRef("pk"), payment_type="balance", status="paid"
    )
    qs = Booking.objects.filter(
        status="confirmed",
        arrival__lte=cutoff,
        balance_due__gt=0,
        stripe_customer_id__isnull=False,
        stripe_payment_method_id__isnull=False,
    ).exclude(Exists(paid_balance))

    enqueued = 0
    for b in qs.iterator(): #el iterator sirve para no cargar la qs entera en memoria, de esta manera los leemos en chunks(trozos) de 2000(valor default, pero se puede cambiar con chunk_size=lo que quieras)
        # Encolar tarea de forma asíncrona (no bloqueante)
        charge_balance_for_booking.delay(b.id, base_url)
        enqueued += 1

    logger.info(f"Encoladas {enqueued} tareas de cobro de balance")
    return f"enqueued={enqueued}"