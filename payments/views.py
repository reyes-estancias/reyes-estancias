from django.shortcuts import render
import stripe
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.timezone import make_aware, now, timedelta
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from properties.models import Property
from bookings.models import Booking, BookingChangeLog
from .models import Payment, RefundLog
from django.core.mail import send_mail
from django.template.loader import render_to_string
from .services import *
from django.db import IntegrityError
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from .services import reschedule_balance_charge, send_booking_confirmation_email

assert settings.STRIPE_SECRET_KEY, "STRIPE_SECRET_KEY no está cargada (None/vacía)"
stripe.api_key = settings.STRIPE_SECRET_KEY

def to_cents(mx_decimal):
    return int(mx_decimal * Decimal("100").quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def _round(num):
    return num.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ─────────────────────────────────────────────
#  HELPERS: lógica compartida por ambas vistas
# ─────────────────────────────────────────────

def _checkout_logic(request, booking):
    """Lógica del depósito inicial (30%). Compartida por StartCheckoutView y StartCheckoutByTokenView."""
    prop = booking.property

    if booking.payments.filter(payment_type="deposit", status="paid"):
        messages.info(request, "El depósito ya está pagado")
        return redirect("bookings_list")

    if booking.hold_expires_at and booking.hold_expires_at <= now():
        messages.info(request, "La reserva ha expirado, vuelve a comprobar disponibilidad")
        return redirect("property_detail", pk=booking.property_id)

    if booking.status != "pending":
        messages.warning(request, "Esta reserva no está en un estado válido para pagar.")
        return redirect("bookings_list")

    checkin  = booking.arrival.date()
    checkout = booking.departure.date()
    quote    = prop.quote_total(checkin, checkout)
    total    = quote["total"]
    deposit  = (total * Decimal("0.30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    balance  = (total - deposit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    fields_to_update = []
    if booking.total_amount   != total:   booking.total_amount   = total;   fields_to_update.append("total_amount")
    if booking.deposit_amount != deposit: booking.deposit_amount = deposit; fields_to_update.append("deposit_amount")
    if booking.balance_due    != balance: booking.balance_due    = balance; fields_to_update.append("balance_due")
    booking.status          = "pending";              fields_to_update.append("status")
    booking.hold_expires_at = now() + timedelta(minutes=30); fields_to_update.append("hold_expires_at")
    if fields_to_update:
        booking.save(update_fields=fields_to_update)

    payment = (booking.payments.filter(payment_type="deposit").order_by("-created_at").first())

    if payment and payment.status == "paid":
        messages.info(request, "El depósito ya está pagado.")
        return redirect("bookings_list")

    if not payment:
        orphan = booking.payments.filter(payment_type="").order_by("-created_at").first()
        if orphan:
            payment = orphan
            payment.payment_type = "deposit"
            payment.amount       = deposit
            payment.currency     = "MXN"
            payment.status       = "pending"
            payment.save(update_fields=["payment_type", "amount", "currency", "status"])

    if not payment or payment.status not in ("pending", "requires_action"):
        payment = Payment.objects.create(
            booking=booking, payment_type="deposit",
            status="pending", amount=deposit, currency="MXN",
        )
    elif payment.amount != deposit:
        payment.amount = deposit
        payment.save(update_fields=["amount"])

    success_url = request.build_absolute_uri(reverse("payment_success")) + f"?booking_id={booking.id}"
    cancel_url  = request.build_absolute_uri(reverse("payment_cancel"))  + f"?booking_id={booking.id}"
    desc = (
        f"Reserva {prop.name} · {checkin} => {checkout} · {booking.person_num} persona(s) · "
        f"Limpieza $100 MXN => Balance · ${balance} MXN · Total ${total} MXN · Anticipo 30%"
    )

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=booking.guest_email or None,
        customer_creation="always",
        payment_intent_data={
            "setup_future_usage": "off_session",
            "metadata": {"booking_id": str(booking.id), "payment_id": str(payment.id), "type": "deposit"},
        },
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "mxn",
                "unit_amount": to_cents(deposit),
                "product_data": {"name": f"Anticipo reserva · {prop.name}", "description": desc},
            },
        }],
        metadata={"booking_id": str(booking.id), "payment_id": str(payment.id), "type": "deposit"},
    )
    payment.stripe_checkout_session_id = session.id
    payment.save(update_fields=["stripe_checkout_session_id"])
    return redirect(session.url)


def _retry_deposit_logic(request, booking):
    """Reintento de depósito. Compartida por RetryDepositPaymentView y RetryDepositByTokenView."""
    prop = booking.property

    if not booking.payments.filter(payment_type="deposit", status="requires_action").exists():
        messages.info(request, "El deposito ya está pagado")
        return redirect("bookings_list")

    if booking.deposit_amount and booking.deposit_amount <= 0:
        messages.error(request, "No hay cargos de deposito pendientes")
        return redirect("bookings_list")

    deposit = booking.deposit_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payment = booking.payments.filter(payment_type="deposit").order_by("-created_at").first()

    if not payment or payment.status not in ("failed", "requires_action", "pending"):
        payment = Payment.objects.create(
            booking=booking, payment_type="deposit",
            amount=deposit, currency="MXN", status="pending",
        )
    elif payment.amount != deposit:
        payment.amount = deposit
        payment.save(update_fields=["amount"])

    success_url = request.build_absolute_uri(reverse("payment_success")) + f"?booking_id={booking.id}"
    cancel_url  = request.build_absolute_uri(reverse("payment_cancel"))  + f"?booking_id={booking.id}"
    desc = (
        f"Reserva {prop.name} · {booking.arrival.date()} → {booking.departure.date()} · "
        f"{booking.person_num} persona(s) · Anticipo 30%"
    )

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=booking.guest_email,
        customer_creation="always",
        payment_intent_data={
            "setup_future_usage": "off_session",
            "metadata": {"booking_id": str(booking.id), "payment_id": str(payment.id), "type": "deposit"},
        },
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "mxn",
                "unit_amount": to_cents(deposit),
                "product_data": {"name": f"Anticipo reserva · {prop.name}", "description": desc},
            },
        }],
        metadata={"booking_id": str(booking.id), "payment_id": str(payment.id), "type": "deposit"},
    )
    payment.stripe_checkout_session_id  = session.id
    payment.stripe_payment_intent_id    = session.payment_intent
    payment.status                      = "pending"
    payment.save(update_fields=["stripe_checkout_session_id", "stripe_payment_intent_id", "status"])
    return redirect(session.url)


def _balance_start_logic(request, booking):
    """Cobro del balance (70%). Compartida por StartBalanceCheckoutView y StartBalanceByTokenView."""
    amount = booking.balance_due_runtime()
    result = charge_offsession_with_fallback(
        booking=booking, request=request, amount=amount,
        payment_type="balance",
        description=f"Pago del balance · {booking.property.name}",
    )
    if result["status"] == "paid":
        messages.info(request, "Balance pagado correctamente")
    elif result["status"] == "requires_action":
        messages.info(request, "Pago requiere acción, revisa tu correo")
    elif result["status"] == "already_paid":
        messages.info(request, "No hay saldo pendiente por cobrar")
    elif result["status"] == "missing_method":
        messages.error(request, "No hay método de pago guardado para ejecutar el cargo.")
    else:
        messages.error(request, f"No se pudo iniciar el cobro: {result.get('error', 'Error desconocido')}")
    return redirect("bookings_list")


def _retry_balance_logic(request, booking):
    """Reintento manual del balance. Compartida por RetryBalancePaymentView y RetryBalanceByTokenView."""
    payment     = booking.payments.filter(payment_type="balance").order_by("-created_at").first()
    success_url = request.build_absolute_uri(reverse("payment_success")) + f"?booking_id={booking.id}"
    cancel_url  = request.build_absolute_uri(reverse("payment_cancel"))  + f"?booking_id={booking.id}"

    if not payment.stripe_checkout_session_id:
        session = stripe.checkout.Session.create(
            mode="payment",
            customer=booking.stripe_customer_id,
            success_url=success_url,
            cancel_url=cancel_url,
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "mxn",
                    "unit_amount": to_cents(booking.balance_due),
                    "product_data": {
                        "name": f"Segundo pago · {booking.property.name}",
                        "description": f"Booking #{booking.id} — {booking.arrival.date()} → {booking.departure.date()}",
                    },
                },
            }],
            metadata={"booking_id": str(booking.id), "payment_id": str(payment.id), "type": "balance"},
        )
        payment.stripe_checkout_session_id = session.id
        payment.save(update_fields=["stripe_checkout_session_id"])
        return redirect(session.url)

    session = stripe.checkout.Session.retrieve(payment.stripe_checkout_session_id)
    return redirect(session.url)


# ─────────────────────────────────────────────
#  VISTAS — camino por access_token (sin login)
# ─────────────────────────────────────────────

class StartCheckoutByTokenView(View):
    def get(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _checkout_logic(request, booking)


class RetryDepositByTokenView(View):
    def get(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _retry_deposit_logic(request, booking)


class StartBalanceByTokenView(View):
    def get(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _balance_start_logic(request, booking)


class RetryBalanceByTokenView(View):
    def get(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _retry_balance_logic(request, booking)


# ─────────────────────────────────────────────
#  SUCCESS / CANCEL (sin LoginRequired — accesibles por cualquiera con booking_id)
# ─────────────────────────────────────────────

class CheckoutSuccesView(View):
    def get(self, request):
        # Poner guest_email en sesión para que BookingsList funcione sin login
        booking_id = request.GET.get("booking_id")
        if booking_id and not request.user.is_authenticated:
            try:
                booking = Booking.objects.get(pk=booking_id)
                if booking.guest_email:
                    request.session["guest_email"] = booking.guest_email
            except Booking.DoesNotExist:
                pass
        messages.success(request, "Pago realizado correctamente")
        return redirect(reverse("bookings_list"))


class CheckoutCancelView(View):
    def get(self, request):
        booking_id = request.GET.get("booking_id")
        payment = Payment.objects.filter(booking_id=booking_id).order_by("-created_at").first()
        if payment and payment.status == "pending":
            payment.status = "failed"
            payment.save(update_fields=["status"])
        try:
            booking = Booking.objects.get(pk=booking_id)
            booking.status = "cancelled"
            booking.save(update_fields=["status"])
            messages.info(request, "Pago fallido, la reserva ha sido cancelada")
        except Booking.DoesNotExist:
            messages.info(request, "Operación cancelada")
        return redirect("bookings_list")


# ─────────────────────────────────────────────
#  WEBHOOK STRIPE
# ─────────────────────────────────────────────

@csrf_exempt
def stripe_webhook(request):
    payload    = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponseBadRequest("Invalid payload or signature")

    etype = event.get("type")
    obj   = event["data"]["object"]

    if etype == "checkout.session.completed":
        session     = obj
        booking_id  = session.get("metadata", {}).get("booking_id")
        payment_id  = session.get("metadata", {}).get("payment_id")
        pi_id       = session.get("payment_intent")
        customer_id = session.get("customer")

        if not (booking_id and payment_id and pi_id):
            return HttpResponse(status=200)

        pi = stripe.PaymentIntent.retrieve(pi_id, expand=["payment_method"])
        customer_id       = pi.get("customer")
        payment_method_id = (
            pi["payment_method"]["id"] if isinstance(pi.get("payment_method"), dict)
            else pi.get("payment_method")
        )

        with transaction.atomic():
            booking = Booking.objects.get(pk=booking_id)
            payment = Payment.objects.get(pk=payment_id, booking=booking)

            if payment.status != "paid":
                payment.status = "paid"
                payment.save(update_fields=["status"])

            change_log_id = (
                (session.get("metadata") or {}).get("change_log_id")
                or (payment.metadata or {}).get("change_log_id")
            )

            update = ["status"]
            was_pending = booking.status != "confirmed"
            if was_pending:
                booking.status = "confirmed"
            if customer_id and booking.stripe_customer_id != customer_id:
                booking.stripe_customer_id = customer_id
                update.append("stripe_customer_id")
            if payment_method_id and booking.stripe_payment_method_id != payment_method_id:
                booking.stripe_payment_method_id = payment_method_id
                update.append("stripe_payment_method_id")

            if payment.payment_type == "deposit" and payment.metadata.get("payment_role") == "deposit_topup":
                if change_log_id:
                    try:
                        clog = BookingChangeLog.objects.select_for_update().get(pk=change_log_id, booking=booking)
                        if clog.status == "pending":
                            booking.arrival       = clog.new_arrival
                            booking.departure     = clog.new_departure
                            booking.total_amount  = _round(clog.new_T)
                            booking.deposit_amount = _round(clog.deposit_target)
                            booking.balance_due   = compute_balance_due_snapshot(booking)
                            update += ["arrival", "departure", "total_amount", "deposit_amount", "balance_due"]
                            clog.status = "applied"
                            clog.save(update_fields=["status"])
                            BookingChangeLog.objects.filter(booking=booking, status="pending").exclude(pk=clog.pk)\
                                .update(status="superseded", superseded_at=now())
                    except BookingChangeLog.DoesNotExist:
                        pass
                (Payment.objects
                    .filter(booking=booking, payment_type="deposit",
                            status__in=["pending", "requires_action"],
                            metadata__payment_role="deposit_topup")
                    .exclude(pk=payment.pk)
                    .update(status="void", superseded_at=now()))
                if "balance_due" not in update:
                    booking.balance_due = compute_balance_due_snapshot(booking)
                    update.append("balance_due")

            elif payment.payment_type == "extension":
                if change_log_id:
                    try:
                        clog = BookingChangeLog.objects.select_for_update().get(pk=change_log_id, booking=booking)
                        if clog.status == "pending":
                            clog.status = "applied"
                            clog.save(update_fields=["status"])
                    except BookingChangeLog.DoesNotExist:
                        pass
                if "balance_due" not in update:
                    booking.balance_due = compute_balance_due_snapshot(booking)
                    update.append("balance_due")

            elif "balance_due" not in update:
                booking.balance_due = compute_balance_due_snapshot(booking)
                update.append("balance_due")

            booking.save(update_fields=update)
            _booking_sched = booking
            transaction.on_commit(
                lambda b=_booking_sched: reschedule_balance_charge(b, b.arrival + timedelta(days=1), settings.SITE_BASE_URL)
            )

            payment.stripe_payment_intent_id = pi_id
            payment.status = "paid"
            payment.save(update_fields=["stripe_payment_intent_id", "status"])

            # Email de confirmación solo cuando el depósito inicial confirma la reserva
            is_initial_deposit = (
                payment.payment_type == "deposit"
                and (payment.metadata or {}).get("payment_role") != "deposit_topup"
            )
            if was_pending and is_initial_deposit:
                _booking_snap = booking
                transaction.on_commit(
                    lambda b=_booking_snap: send_booking_confirmation_email(b, base_url=settings.SITE_BASE_URL)
                )

    elif etype == "payment_intent.payment_failed":
        pi         = obj
        pi_id      = pi.get("id")
        booking_id = pi.get("metadata", {}).get("booking_id")
        payment_id = pi.get("metadata", {}).get("payment_id")
        try:
            booking = Booking.objects.get(pk=booking_id)
            payment = Payment.objects.get(pk=payment_id, booking=booking)
        except (Booking.DoesNotExist, Payment.DoesNotExist):
            return HttpResponse(status=200)
        payment.stripe_payment_intent_id = pi_id
        payment.save(update_fields=["stripe_payment_intent_id"])
        payment = Payment.objects.filter(stripe_payment_intent_id=pi_id).select_related("booking").first()
        if payment:
            payment.status = "requires_action"
            payment.save(update_fields=["status"])

    elif etype == "payment_intent.succeeded":
        pi         = obj
        pi_id      = pi.get("id")
        booking_id = (pi.get("metadata") or {}).get("booking_id")
        payment_id = (pi.get("metadata") or {}).get("payment_id")
        if not (booking_id and payment_id):
            return HttpResponse(status=200)
        try:
            booking = Booking.objects.get(pk=booking_id)
            payment = Payment.objects.get(pk=payment_id, booking=booking)
        except (Booking.DoesNotExist, Payment.DoesNotExist):
            return HttpResponse(status=200)
        if payment.status == "paid":
            return HttpResponse(status=200)
        with transaction.atomic():
            payment.stripe_payment_intent_id = pi_id
            payment.status = "paid"
            payment.save(update_fields=["stripe_payment_intent_id", "status"])
            booking.balance_due = compute_balance_due_snapshot(booking)
            booking.save(update_fields=["balance_due"])

    elif etype in ("refund.updated", "charge.refunded"):
        refunds = []
        if etype == "refund.updated" and obj.get("object") == "refund":
            refunds = [obj]
        elif etype == "charge.refunded" and obj.get("object") == "charge":
            refunds = obj.get("refunds", {}).get("data", [])

        for refund in refunds:
            payment_id = (refund.get("metadata") or {}).get("payment_id")
            if not payment_id:
                pi_id = refund.get("payment_intent")
                if pi_id:
                    try:
                        pi = stripe.PaymentIntent.retrieve(pi_id)
                        payment_id = (pi.get("metadata") or {}).get("payment_id")
                    except Exception:
                        payment_id = None
            if not payment_id:
                continue
            amount_mxn   = Decimal(refund.get("amount", 0)) / Decimal("100")
            status       = refund.get("status")
            try:
                RefundLog.objects.create(
                    stripe_refund_id=refund["id"], payment_id=payment_id, amount=amount_mxn,
                )
            except IntegrityError:
                continue
            refund_status = "paid" if status == "succeeded" else "failed" if status == "failed" else "pending"
            updates = {
                "refund_count":  Coalesce(F("refund_count"), Value(0)) + 1,
                "refund_status": refund_status,
                "stripe_refund_id": refund["id"],
                "last_refund_at": now(),
            }
            if refund_status == "paid":
                updates["refunded_amount"] = Coalesce(F("refunded_amount"), Value(Decimal("0.00"))) + amount_mxn
            Payment.objects.filter(pk=payment_id).update(**updates)

    return HttpResponse(status=200)


def expire_unpaid_bookings():
    qs      = Booking.objects.filter(status="pending", hold_expires_at__isnull=False, hold_expires_at__lt=now())
    updated = qs.update(status="expired")
    return updated
