from decimal import Decimal, ROUND_HALF_UP
import stripe
from django.conf import settings
from django.template.loader import render_to_string
from django.core.mail import send_mail
from django.urls import reverse
from django.db import transaction
from django.utils.timezone import now
from datetime import datetime, time, date, timedelta
from django.shortcuts import get_object_or_404, redirect
from .models import Payment
from django.db.models import Sum
from properties.models import Property
from django.db.models import Sum, Q, Value, F
from django.db.models.functions import Coalesce
from django.utils import timezone
from celery.result import AsyncResult
from django.conf import settings

DEPOSIT_RATE = Decimal("0.30")
HALF_RATE    = Decimal("0.50")
FULL_RATE    = Decimal("1.00")
TOPUP_TTL_MIN = 10

stripe.api_key = settings.STRIPE_SECRET_KEY

def _build_success_cancel(booking, request=None, base_url=None):
    base = None
    if request is not None:
        # quita barra final; build_absolute_uri('/') te da la base con slash
        base = request.build_absolute_uri('/').rstrip('/')
    else:
        base = (base_url or getattr(settings, "SITE_BASE_URL", None) or "http://127.0.0.1:8000").rstrip('/')
    success_url = f"{base}{reverse('payment_success')}?booking_id={booking.id}"
    cancel_url  = f"{base}{reverse('payment_cancel')}?booking_id={booking.id}"
    return success_url, cancel_url

def _to_cents(mx: Decimal) -> int:
    return int((mx * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def ensure_balance_payment(booking, payment_type, amount):
    with transaction.atomic():
        p = (booking.payments
             .select_for_update()
             .filter(payment_type=payment_type, status__in=["pending","requires_action"])
             .order_by("-id")
             .first())
        if p:
            if p.amount != amount:
                p.amount = amount
                p.save(update_fields=["amount"])
            return p

        return Payment.objects.create(
            booking=booking,
            payment_type=payment_type,
            status="pending",
            amount=amount,
            currency="MXN",
        )

def charge_offsession_with_fallback(
        booking, request = None,
        amount = None, 
        payment_type = "balance",
        description = "Saldo pendiente", *, base_url: str | None = None):
    """
    Intenta cobrar el saldo (70%) off-session. Si falla, crea Checkout Session y envía email con el link.
    Retorna un string corto con el resultado: "paid" | "requires_action" | "failed".
    """
    payment = ensure_balance_payment(booking, payment_type=payment_type, amount=amount)

    if payment.status == "paid":
        return {"status": "already_paid", "payment": payment}

    if amount <= 0:
        return {"status": "skipped", "msg": "Importe cero"}

    if not booking.balance_due or booking.balance_due <= 0:
        return {"status": "no_balance", "payment": payment}
    
    if not (booking.stripe_customer_id and booking.stripe_payment_method_id):
        return {"status": "missing_method", "payment": payment}

    try:
        intent = stripe.PaymentIntent.create(
            amount=_to_cents(amount),
            currency="mxn",
            customer=booking.stripe_customer_id,
            payment_method=booking.stripe_payment_method_id,
            off_session=True,
            confirm=True,
            metadata={
                "booking_id": str(booking.id),
                "payment_id": str(payment.id),
                "type":payment_type
            },
            description=description,
            idempotency_key=f"{payment_type}-{payment.id}",
        )
        

        payment.stripe_payment_intent_id = intent.id 


        #Si se puede realizar el pago:
        if intent.status == "succeeded":
            payment.status = "paid"
            payment.save(update_fields=["stripe_payment_intent_id", "status"])
            return {"status": "paid", "payment": payment, "intent_id": intent.id}
        
        
        #Si no se puede:
        # Con confirm=True, si no es succeeded casi siempre Stripe lanza excepción.
        # Aun así, si llegas aquí, trata como requiere acción:
        payment.status = "requires_action"
        payment.save(update_fields=["stripe_payment_intent_id", "status"])

    except stripe.error.CardError as e:
        # ⇒ aquí caes cuando necesita 3DS o la tarjeta fue rechazada, porque stripe, en vez de devolver
        # status "requires_action", lanza excepción de CardError.
        #Obtenemos el objeto del error que lanza
        err = getattr(e, "error", None)
        #Dentro del error, suele venir el Payment intent incompleto
        pi = getattr(err, "payment_intent", None)

        # Normalizamos el ID del PaymentIntent (a veces viene dict, a veces objeto)
        pi_id = (pi.get("id") if isinstance(pi, dict) else getattr(pi, "id", None))

        # Si hay ID, lo guardamos en nuestro modelo
        if pi_id:
            payment.stripe_payment_intent_id = pi_id

        # En vez de poner "failed", lo marcamos como "requires_action"
        payment.status = "requires_action"
        payment.save(update_fields=["stripe_payment_intent_id", "status"] if pi_id else ["status"])

        #En caso de que sea algún error real de stripe: Servidor, validación etc...
    except Exception as e:
        payment.status = "failed"
        payment.save(update_fields=["status"])
        return {"status": "failed", "payment": payment, "error": str(e)}
        

    #Si no ha entrado en el if de arriba, significa que no se ha podido realizar el pago,
    #y si no ha entrado en el exept de justo encima, significa que ha dado Card.error
    #Continuamos con el flujo creamos sesión y le mandamos link paga que pague manualmente
    success_url, cancel_url = _build_success_cancel(booking, request=request, base_url=base_url)

    session = stripe.checkout.Session.create(
        mode="payment",
        customer=booking.stripe_customer_id,
        success_url=success_url,
        cancel_url=cancel_url,
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "mxn",
                "unit_amount": _to_cents(amount),
                "product_data": {
                    "name": description,
                    "description": f"Booking #{booking.id} — {booking.arrival.date()} → {booking.departure.date()}",
                },
            },
        }],
        metadata={"booking_id": str(booking.id), "payment_id": str(payment.id), "type": payment_type},
    )
    payment.status = "requires_action"
    payment.stripe_checkout_session_id = session.id
    payment.save(update_fields=["stripe_checkout_session_id", "status"])

    
    #Enviamos email
    context = {
            "user": booking.user,
            "booking": booking,
            "payment_url": session.url #nombre en el template
    }

    subject = f"Completa tu pago {description}"
    html_body = render_to_string("emails/retry_balance_payment.html", context)
    
    send_mail(
        subject=subject,
        message="Para completar tu pago haz click en el enlace (HTML only).",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[booking.user.email],
        html_message=html_body,
        fail_silently=False,
    )
    return {"status": "requires_action", "payment": payment, "checkout_url": session.url}


def reschedule_balance_charge(booking, when, base_url: str | None = None):
    
    from payments.tasks import charge_balance_for_booking

    from django.utils import timezone
    from django.conf import settings

    """
    Programa (o reprograma) el cobro del balance para un booking en 'when' (ETA).
    Guarda task_id y ETA en el modelo para poder revocarla si cambian las fechas.
    """

    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())

    base_str = str(base_url) if isinstance(base_url, Decimal) else (None if base_url is None else str(base_url))
    #Revocar tareas anteriores:

    if getattr(booking, "balance_charge_task_id", None):
        try:
            AsyncResult(booking.balance_charge_task_id).revoke()
        except Exception:
            pass

    #Encolar ETA
    result = charge_balance_for_booking.apply_async(args=[booking.pk, base_str], eta=when)

    #Tracking de la task:
    booking.balance_charge_task_id = result.id
    booking.balance_charge_eta = when
    booking.save(update_fields=["balance_charge_task_id", "balance_charge_eta"])

    return {"scheduled_for": when, "task_id": result.id}


#####################################################################################################################
#                                             REEMBOLSOS                                                            #
#####################################################################################################################

def compute_refund_plan(booking):
    """
    Devuelve una lista de {'payment': Payment, 'amount': Decimal} en MXN,
    sin efectos secundarios. Política:
      - >7 días antes del check-in: reembolso total del depósito pagado.
      - 0..7 días: no hay reembolso se cobra el 50% de penalización.
      - no show / ya pasó el check-in: no hay reembolso.
    """
    from bookings.models import BookingChangeLog

    today = now().date()

    # Anti-fraude: si el usuario retrasó el check-in en los últimos 7 días,
    # usamos la fecha original más temprana para calcular la ventana de reembolso.
    # Esto evita que alguien mueva las fechas al futuro para obtener reembolso del 100%
    # y luego cancele inmediatamente.
    fraud_window = now() - timedelta(days=7)
    early_change = (
        BookingChangeLog.objects
        .filter(
            booking=booking,
            status="applied",
            created_at__gte=fraud_window,
            new_arrival__gt=F("old_arrival"),
        )
        .order_by("old_arrival")
        .first()
    )
    effective_arrival = early_change.old_arrival if early_change else booking.arrival

    days_before = (effective_arrival.date() - today).days
    
    already_paid_total = booking.payments.filter(status="paid").aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    total = booking.total_amount

    refund = []
    penalty = Decimal("0.00")
    penalty_type = None

    if days_before < 0:
        window = "noshow" 
        target = total * FULL_RATE
        penalty = max(target - already_paid_total, Decimal("0.00"))
        penalty_type = "no_show" if penalty > 0 else None
    
    elif days_before <= 7:
        window = "lte7"
        target = total * HALF_RATE
        penalty = max(target - already_paid_total, Decimal("0.00"))
        penalty_type = "cancellation_fee" if penalty > 0 else None

    else:
        #Se devuelve todo el depósito.
        window = "gt7"
        payment = booking.payments.filter(payment_type="deposit", status="paid").last()
        if payment:
            refund_amount = payment.amount
            already_refunded = (getattr(payment, "refunded_amount", Decimal(0.00)))
            remaining = max(refund_amount - already_refunded, Decimal("0.00"))
            
            if remaining > 0:
                refund.append({"payment": payment, "amount": remaining})


    return {
        "window" : window,
        "days_before" : days_before,
        "refunds" : refund,
        "penalty" : penalty,
        "penalty_type" : penalty_type
    }

def refund_payment(payment, amount, reason="requested_by_customer"):

    if amount is None or amount <= 0:
        return None

    if not payment.stripe_payment_intent_id:
        return None
    
    refunded = getattr(payment, "refunded_amount", Decimal("0.00"))
    remaining = max(payment.amount - refunded, Decimal("0.00"))
    if remaining <= 0:
        None

    if amount > remaining:
        amount = remaining

    if amount <= 0:
        return None
    try:
        if payment.refund_status == "none":
            payment.refund_status = "pending"
            payment.refund_reason = reason
            payment.save(update_fields=["refund_status", "refund_reason"])
        
        refund = stripe.Refund.create(
            payment_intent=payment.stripe_payment_intent_id,
            amount=_to_cents(amount),
            reason=reason,
            metadata={"payment_id": str(payment.id), "booking_id" : str(payment.booking.id)}
        )
        return refund
    except stripe.error.InvalidError as e:
        msg = getattr(e, "user_message", None) or str(e)
        code = getattr(e, "code", None)
        param = getattr(e, "param", None)
        # Puedes devolver un dict o raise; para pruebas mejor devolver info
        return {"error": "invalid_request", "message": msg, "code": code, "param": param}
    
def _round(x):
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def has_current_pending_deposit_topup(booking) -> bool:
    from payments.models import Payment
    return Payment.objects.filter(
        booking=booking,
        payment_type="deposit",
        status__in=["pending", "requires_action"],
        metadata__payment_role="deposit_topup",
    ).exists()

def get_paid_deposit_amount(booking):
    """
    Suma de depósitos 'paid' (payment_type='deposit') menos lo ya reembolsado.
    Soporta múltiples depósitos (p.ej., top-ups) usando el mismo tipo.
    """
    deposits = booking.payments.filter(payment_type="deposit", status="paid")
    paid = deposits.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    refunded = Decimal("0.00")
    for deposit in deposits.only("id", "amount", "refunded_amount"):
        refunded += getattr(deposit, "refunded_amount", Decimal("0.00"))
    return _round(Decimal(paid) - refunded)

def create_deposit_topup_checkout(booking, request, amount, 
    description="Depósito adicional para el cambio de fechas",*, change_log_id):

    """
    Crea un Payment (payment_type='deposit') y una Checkout Session para cobrar SOLO el top-up.
    No envía emails ni cambia el status a requires_action; dejamos 'pending' y el webhook marcará 'paid'.
    """

    if amount <= 0:
        return {"status": "skipped", "msg": "Importe adicional no requerido"}
    
    with transaction.atomic():
        recent_threshold = now() - timedelta(minutes=TOPUP_TTL_MIN)
        
        prev = (Payment.objects.select_for_update().filter(
            booking=booking,
            payment_type="deposit",
            status__in=["pending", "requires_action"],
            metadata__payment_role="deposit_topup").order_by("-created_at").first())
    if (prev and prev.amount == amount and prev.metadata.get("change_log_id") == change_log_id and prev.stripe_checkout_session_id):
        try:
            session = stripe.checkout.Session.retrieve(prev.stripe_checkout_session_id)
            return {"status": "pending", "payment":prev, "checkout_url": session.url}
        except Exception:
            pass
    
    if prev and prev.metadata.get("change_log_id") != change_log_id and prev.stripe_checkout_session_id:
        try:
            stripe.checkout.Session.expire(prev.stripe_checkout_session_id)
        except Exception:
            pass

    (Payment.objects.filter(
        booking=booking,
        payment_type="deposit",
        metadata__payment_role="deposit_topup",
        status__in=["pending", "requires_action"],
        ).exclude(metadata__change_log_id=change_log_id).update(status="superseded", superseded_at=now()))
    
    #rellenamos nuevos campos
    client_ref = f"booking:{booking.id}, topup:{int(now().timestamp())}"
    #idempotencia
    bucket = int(now().timestamp() // (TOPUP_TTL_MIN * 60))
    idem_key = f"topup:{booking.id}:{_to_cents(amount)}:clog:{change_log_id}"


    payment = Payment.objects.create(
        booking=booking,
        payment_type="deposit",
        amount=amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        currency="MXN",
        status="pending",
        metadata={"payment_role":"deposit_topup", "change_log_id": change_log_id},
        client_reference_id=client_ref,
        idempotency_key=idem_key,
        expires_at=now() + timedelta(minutes=TOPUP_TTL_MIN),
    )

    success_url = request.build_absolute_uri(reverse("payment_success")) + f"?booking_id={booking.id}"
    cancel_url = request.build_absolute_uri(reverse("payment_cancel")) + f"?booking_id={booking.id}"

    session = stripe.checkout.Session.create(
        mode="payment",
        customer=booking.stripe_customer_id or None,
        success_url=success_url,
        cancel_url=cancel_url,
        payment_intent_data={
            "setup_future_usage":"off_session",
            "metadata":{"booking_id":str(booking.id), 
                        "payment_id":str(payment.id),
                        "type": "deposit",
                        "payment_role":"deposit_topup",
                        "change_log_id": str(change_log_id)},

        },
        line_items=[{
            "quantity":1,
            "price_data":{
                "currency":"mxn",
                "unit_amount":_to_cents(amount),
                "product_data":{
                    "name":description,
                    "description":f"Booking #{booking.id} — {booking.arrival.date()} → {booking.departure.date()}",
                },              
            },
        }],
        metadata={
            "booking_id":str(booking.id),
            "payment_id": str(payment.id),
            "type": "deposit",
            "payment_role": "deposit_topup",
            "change_log_id": str(change_log_id),
        },
        idempotency_key=idem_key,
    )

    payment.stripe_checkout_session_id = session.id
    payment.stripe_payment_intent_id = session.payment_intent
    payment.save(update_fields=["stripe_checkout_session_id", "stripe_payment_intent_id"])

    return {"status": "pending", "payment":payment, "checkout_url": session.url}

def trigger_refund_for_reduction(booking, amount):
    """
    Reembolsa `amount` MXN cuando se reduce la estancia con el balance ya pagado.
    Busca pagos en orden: extension → balance → deposit (más reciente primero).
    Stripe exige un refund por PaymentIntent, por lo que puede generar varios refunds.
    """
    remaining = _round(amount or Decimal("0.00"))
    result = []

    if remaining <= 0:
        return result

    for ptype in ("extension", "balance", "deposit"):
        if remaining <= 0:
            break
        payments = booking.payments.filter(payment_type=ptype, status="paid").order_by("-id")
        for payment in payments:
            if remaining <= 0:
                break
            refunded = getattr(payment, "refunded_amount", Decimal("0.00"))
            available = max(payment.amount - refunded, Decimal("0.00"))
            if available <= 0:
                continue
            to_refund = _round(min(available, remaining))
            refund = refund_payment(payment, to_refund, reason="requested_by_customer")
            result.append({"payment_id": payment.id, "requested": to_refund, "result": refund})
            remaining -= to_refund

    return result


def compute_balance_due_snapshot(booking) -> Decimal:
    agg = Payment.objects.filter(booking=booking).aggregate(
        dep=Coalesce(Sum("amount", filter=Q(payment_type="deposit", status="paid")), Decimal("0.00")),
        bal=Coalesce(Sum("amount", filter=Q(payment_type="balance", status="paid")), Decimal("0.00")),
        ext=Coalesce(Sum("amount", filter=Q(payment_type="extension", status="paid")), Decimal("0.00")),
        ref=Coalesce(Sum("refunded_amount", filter=Q(refund_status="paid")), Decimal("0.00")),
    )
    total_paid_net = agg["dep"] + agg["bal"] + agg["ext"] - agg["ref"]
    return _round(max(booking.total_amount - total_paid_net, Decimal("0.00")))
