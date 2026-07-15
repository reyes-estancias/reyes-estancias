from django.shortcuts import render
from django.views.generic.list import ListView
from django.views import View
from .models import *
from properties.models import *
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.contrib import messages
from django.shortcuts import redirect, get_object_or_404
from django.utils.timezone import make_aware
from datetime import datetime
from django.urls import reverse
from decimal import Decimal, ROUND_HALF_UP
from core.tzutils import compose_aware_dt
from payments.services import *
from .forms import ChangeDatesForm, GuestInfoForm, RequestMagicLinkForm
from .services import *
from django.http import HttpResponse, HttpResponseBadRequest
from django.db.models import OuterRef, Subquery, Q
from .models import Booking, BookingChangeLog
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from datetime import timedelta
from django.db import transaction
import logging

logger = logging.getLogger(__name__)
#from core.tzutils import compose_aware_dt 
# Create your views here.

class BookingsList(ListView):
    model                = Booking
    template_name        = "bookings/bookings_list.html"
    context_object_name  = "bookings"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated and not request.session.get("guest_email"):
            return redirect("request_magic_link")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        latest_log = (BookingChangeLog.objects
                      .filter(booking=OuterRef("pk"))
                      .order_by("-created_at"))
        base = (Booking.objects
                .annotate(last_deposit_refund=Subquery(latest_log.values("deposit_refund")[:1]))
                .select_related("property")
                .order_by("-arrival"))

        if self.request.user.is_authenticated:
            q = Q(user=self.request.user)
            # Vinculación virtual: si el email está verificado, también mostramos
            # las reservas hechas como invitado con ese mismo email.
            if getattr(self.request.user, "email_verified", False):
                q |= Q(guest_email=self.request.user.email)
            return base.filter(q).distinct()

        guest_email = self.request.session.get("guest_email", "")
        return base.filter(guest_email=guest_email)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            ctx["viewer_email"]     = self.request.user.email
            ctx["is_guest_session"] = False
            ctx["email_verified"]   = getattr(self.request.user, "email_verified", False)
        else:
            ctx["viewer_email"]     = self.request.session.get("guest_email", "")
            ctx["is_guest_session"] = True
            ctx["email_verified"]   = False
        return ctx


class ClearGuestSessionView(View):
    """Limpia la sesión de invitado (magic link) y redirige al formulario."""
    def post(self, request):
        request.session.pop("guest_email", None)
        return redirect("request_magic_link")
    
class CreateBookingView(View):

    def _booking_context(self, request, property_id, params):
        """Parsea fechas, propiedad y precio. Devuelve (ctx_dict, redirect) — uno de los dos será None."""
        checkin      = params.get("checkin")
        checkout     = params.get("checkout")
        cant_personas = params.get("cant_personas")

        if not (checkin and checkout and cant_personas):
            messages.warning(request, "Elija fechas y cantidad de personas")
            return None, redirect("property_detail", pk=property_id)

        try:
            checkin_dt  = compose_aware_dt(checkin, hour=15, minute=0)
            checkout_dt = compose_aware_dt(checkout, hour=12, minute=0)
        except Exception:
            messages.error(request, "Fechas inválidas")
            return None, redirect("property_detail", pk=property_id)

        try:
            prop = Property.objects.get(pk=property_id)
        except Property.DoesNotExist:
            messages.error(request, "Propiedad no encontrada")
            return None, redirect("home")

        try:
            quote = prop.quote_total(checkin_dt.date(), checkout_dt.date())
        except Exception:
            messages.error(request, "Error al calcular el precio")
            return None, redirect("property_detail", pk=property_id)

        total   = quote["total"]
        deposit = (total * Decimal("0.30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        balance = (total - deposit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return {
            "property":     prop,
            "checkin":      checkin,
            "checkout":     checkout,
            "cant_personas": cant_personas,
            "checkin_dt":   checkin_dt,
            "checkout_dt":  checkout_dt,
            "quote":        quote,
            "deposit":      deposit,
            "balance":      balance,
        }, None

    def get(self, request, property_id):
        ctx, err = self._booking_context(request, property_id, request.GET)
        if err:
            return err

        initial = {}
        if request.user.is_authenticated:
            u = request.user
            initial = {
                "first_name": u.first_name,
                "last_name":  u.last_name,
                "email":      u.email,
                "phone":      u.phone,
            }

        return render(request, "bookings/create_booking.html", {
            **ctx,
            "form": GuestInfoForm(initial=initial),
        })

    def post(self, request, property_id):
        ctx, err = self._booking_context(request, property_id, request.POST)
        if err:
            return err

        form = GuestInfoForm(request.POST)
        if not form.is_valid():
            return render(request, "bookings/create_booking.html", {**ctx, "form": form})

        guest_name  = form.guest_name()
        guest_email = form.cleaned_data["email"]
        guest_phone = form.cleaned_data["phone"]

        prop         = ctx["property"]
        checkin_dt   = ctx["checkin_dt"]
        checkout_dt  = ctx["checkout_dt"]
        checkin      = ctx["checkin"]
        checkout     = ctx["checkout"]
        cant_personas = ctx["cant_personas"]
        total        = ctx["quote"]["total"]
        deposit      = ctx["deposit"]
        balance      = ctx["balance"]

        try:
            with transaction.atomic():
                prop = Property.objects.select_for_update().get(pk=property_id)

                if not prop.is_available(checkin, checkout, cant_personas):
                    messages.warning(request, "La propiedad ya no está disponible")
                    url = f"{reverse('property_detail', kwargs={'pk': property_id})}?checkin={checkin}&checkout={checkout}&cant_personas={cant_personas}"
                    return redirect(url)

                conflicting = Booking.objects.filter(
                    property=prop,
                    status__in=["confirmed", "pending"],
                    arrival__lt=checkout_dt,
                    departure__gt=checkin_dt,
                ).exclude(hold_expires_at__lt=now()).exists()

                if conflicting:
                    messages.warning(request, "La propiedad ya no está disponible")
                    url = f"{reverse('property_detail', kwargs={'pk': property_id})}?checkin={checkin}&checkout={checkout}&cant_personas={cant_personas}"
                    return redirect(url)

                booking, created = Booking.objects.get_or_create(
                    guest_email=guest_email,
                    property=prop,
                    arrival=checkin_dt,
                    departure=checkout_dt,
                    status="pending",
                    defaults={
                        "guest_name":  guest_name,
                        "guest_phone": guest_phone,
                        "user":        request.user if request.user.is_authenticated else None,
                        "person_num":  int(cant_personas),
                        "total_amount": total,
                        "deposit_amount": deposit,
                        "balance_due": balance,
                        "hold_expires_at": now() + timedelta(minutes=30),
                    },
                )

                if not created:
                    update_fields = []
                    if booking.total_amount != total:
                        booking.total_amount = total
                        update_fields.append("total_amount")
                    if booking.deposit_amount != deposit:
                        booking.deposit_amount = deposit
                        update_fields.append("deposit_amount")
                    if booking.balance_due != balance:
                        booking.balance_due = balance
                        update_fields.append("balance_due")
                    if update_fields:
                        booking.save(update_fields=update_fields)

                return redirect("payment_start_token", token=booking.access_token)

        except Property.DoesNotExist:
            messages.error(request, "Propiedad no encontrada")
            return redirect("home")
        except Exception as e:
            messages.error(request, "Error al procesar la reserva. Inténtelo de nuevo.")
            logger.error(
                f"Error creando reserva: property_id={property_id}, guest_email={guest_email}, error={e}",
                exc_info=True,
            )
            return render(request, "bookings/create_booking.html", {**ctx, "form": form})
    
class RemakeBookingView(LoginRequiredMixin, View):
    login_url = "login"

    def post(self, request, *args, **kwargs):
        booking = get_object_or_404(Booking, pk=kwargs.get("pk"))
        property = booking.property
        
        if booking.user != self.request.user and not self.request.user.is_staff:
            messages.error = (request, "Usuario no autorizado para rehacer la reserva")
            return redirect("home")

        elif booking.status != "cancelled":
            messages.error(request, "Solo se pueden rehacer reservas canceladas")
            return redirect("bookings_list")

        checkin = booking.arrival
        checkout = booking.departure
        cant_personas = booking.person_num

        total_amount = booking.total_amount
        deposit_amount = booking.deposit_amount
        balance_due = booking.balance_due
        status = "pending"
        stripe_customer_id = booking.stripe_customer_id
        stripe_payment_method_id = booking.stripe_payment_method_id

        #Comprobar si existe una rebooking en curso
        existing = (Booking.objects.filter(user=self.request.user, property=property, arrival=checkin,
                                            departure=checkout, status="pending").exclude(hold_expires_at__lt=now()).first())

        if existing:
            messages.error(request, "Ya existía un reintento de reserva, hemos recuperado ese intento para rehacer la reserva cancelada")
            return redirect("payment_start_token", token=existing.access_token)

        if not property.is_available(checkin, checkout, cant_personas):
            messages.error(request, "Las fechas ya no están disponibles")
            return redirect("bookings_list")
        try:
            with transaction.atomic():
                (property.bookings.select_for_update(skip_locked=True)
                .filter(status=["confirmed","pending"]).exclude(hold_expires_at__lt=now())
                .filter(arrival__lt=checkout, departure__gt=checkin).count())

                if not property.is_available(checkin, checkout, cant_personas):
                    messages.error(request, "Las fechas ya no están disponibles")
                    return redirect("bookings_list")

                new_booking = Booking.objects.create(
                    user=request.user,
                    property=property,
                    person_num=cant_personas,
                    arrival=checkin,
                    departure=checkout,
                    total_amount=total_amount,
                    deposit_amount=deposit_amount,
                    balance_due=balance_due,
                    status=status,
                    hold_expires_at=now() + timedelta(minutes=30),
                    stripe_customer_id=stripe_customer_id,
                    stripe_payment_method_id=stripe_payment_method_id,
                    guest_name=booking.guest_name or f"{request.user.first_name} {request.user.last_name}".strip(),
                    guest_email=booking.guest_email or request.user.email,
                    guest_phone=booking.guest_phone or getattr(request.user, "phone", ""),
                )
        except Exception as e:
            messages.error(request, f"No es posible rehacer la reserva: {e}")
            return redirect("bookings_list")

        return redirect("payment_start_token", token=new_booking.access_token)
    


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS compartidos por vistas de usuario y vistas por token
# ─────────────────────────────────────────────────────────────────────────────

def _cancel_logic(request, booking_pk):
    """Ejecuta la cancelación de una reserva. La autorización la gestiona el caller."""
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking_pk)

        if booking.status == "cancelled":
            messages.info(request, "La reserva ya estaba cancelada")
            return redirect("bookings_list")

        plan              = compute_refund_plan(booking)
        task_id_to_revoke = booking.balance_charge_task_id
        booking.status              = "cancelled"
        booking.balance_charge_task_id = None
        booking.balance_charge_eta  = None
        booking.save(update_fields=["status", "balance_charge_task_id", "balance_charge_eta"])

        pending_balances = list(
            Payment.objects.filter(
                booking=booking,
                payment_type__in=["balance", "extension"],
                status__in=["pending", "requires_action"],
            ).values_list("id", "stripe_checkout_session_id")
        )
        if pending_balances:
            Payment.objects.filter(id__in=[pid for pid, _ in pending_balances]) \
                .update(status="void", superseded_at=timezone.now())

    if task_id_to_revoke:
        try:
            AsyncResult(task_id_to_revoke).revoke()
        except Exception:
            pass

    for _, session_id in pending_balances:
        if session_id:
            try:
                stripe.checkout.Session.expire(session_id)
            except Exception:
                pass

    penalty = plan["penalty"]
    if penalty and penalty > 0:
        desc = "Penalización cancelación" if plan["penalty_type"] == "cancellation_fee" else "No show"
        charge_offsession_with_fallback(
            booking=booking, request=request, amount=penalty,
            payment_type=plan["penalty_type"],
            description=f"{desc} · {booking.property.name}",
        )

    def do_refunds():
        for item in plan.get("refunds", []):
            try:
                result = refund_payment(item["payment"], item["amount"])
                if isinstance(result, dict) and "error" in result:
                    logger.error("Reembolso fallido para booking %s, payment %s: %s",
                                 booking.id, item["payment"].id, result)
            except Exception as e:
                logger.error("Excepción al reembolsar booking %s, payment %s: %s",
                             booking.id, item["payment"].id, e)

    transaction.on_commit(do_refunds)
    return redirect("bookings_list")


def _change_dates_start(request, booking, token=None):
    if booking.status == "completed":
        messages.error(request, "No se pueden modificar las fechas de una reserva ya finalizada")
        return redirect("bookings_list")
    if booking.status != "confirmed":
        messages.error(request, "Solo se pueden modificar las fechas de reservas confirmadas")
        return redirect("bookings_list")
    form = ChangeDatesForm(initial={"checkin": booking.arrival, "checkout": booking.departure})
    return render(request, "bookings/change_dates_form.html",
                  {"booking": booking, "form": form, "token": token})


def _change_dates_preview(request, booking, token=None):
    form = ChangeDatesForm(request.POST)
    if not form.is_valid():
        messages.error(request, "El formulario no es válido")
        return render(request, "bookings/change_dates_form.html",
                      {"booking": booking, "form": form, "token": token})

    new_in  = compose_aware_dt(form.cleaned_data["checkin"], 15, 0)
    new_out = compose_aware_dt(form.cleaned_data["checkout"], 12, 0)
    q = quote_change_booking_dates(booking, new_in, new_out)

    if not q["ok"]:
        messages.error(request, "Propiedad no disponible en estas fechas")
        return redirect("booking_change_dates_start_token", token=token) if token \
            else redirect("booking_change_dates_start", pk=booking.id)

    ctx = {
        "booking": booking, "form": form, "quote": q,
        "checkin": form.cleaned_data["checkin"],
        "checkout": form.cleaned_data["checkout"],
        "token": token,
    }
    return render(request, "bookings/change_dates_preview.html", ctx)


def _change_dates_apply(request, booking, actor_user, token=None):
    checkin  = request.POST.get("checkin")
    checkout = request.POST.get("checkout")

    try:
        new_in  = compose_aware_dt(checkin, 15, 0)
        new_out = compose_aware_dt(checkout, 12, 0)
    except Exception as e:
        messages.error(request, "Formulario inválido")
        logger.warning(f"Error parseando fechas en cambio de reserva: {e}",
                       extra={"booking_id": booking.id})
        return redirect("booking_change_dates_start_token", token=token) if token \
            else redirect("booking_change_dates_start", pk=booking.id)

    serv = apply_change_booking_dates(
        booking=booking, new_in=new_in, new_out=new_out,
        actor_user=actor_user, request=request,
    )

    if not serv["ok"]:
        messages.error(request, "No se pudo hacer el cambio de fechas, la disponibilidad cambió")
        return redirect("booking_change_dates_start_token", token=token) if token \
            else redirect("booking_change_dates_start", pk=booking.id)

    actions = serv.get("actions", {})

    if "checkout_url" in actions and "dep_topup" in actions:
        messages.info(request, f"El cambio ha sido aplicado, se necesita un depósito adicional de {actions['dep_topup']} MXN")
        return redirect(actions["checkout_url"])
    if "checkout_url" in actions and "extension_charge" in actions:
        messages.info(request, f"Fechas actualizadas. Pago de extensión pendiente: {actions['extension_charge']} MXN")
        return redirect(actions["checkout_url"])
    if "extension_charge" in actions:
        messages.success(request, f"Extensión aplicada y cobrada correctamente: {actions['extension_charge']} MXN")
        return redirect("bookings_list")
    if "dep_refund" in actions:
        messages.success(request, f"Cambios aplicados. El reembolso ha sido creado: {actions['dep_refund']} MXN")
        return redirect("bookings_list")
    messages.success(request, "Cambio realizado correctamente. Todo listo")
    return redirect("bookings_list")


# ─────────────────────────────────────────────────────────────────────────────
#  VISTAS POR TOKEN — cancelación
# ─────────────────────────────────────────────────────────────────────────────

class CancelBookingByTokenView(View):
    def post(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _cancel_logic(request, booking.pk)


class CancelBookingSureByTokenView(View):
    def get(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return render(request, "bookings/cancel_booking_sure.html", {
            "booking": booking,
            "token": str(token),
        })


# ─────────────────────────────────────────────────────────────────────────────
#  VISTAS POR TOKEN — cambio de fechas
# ─────────────────────────────────────────────────────────────────────────────

class BookingChangeDatesStartByTokenView(View):
    def get(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _change_dates_start(request, booking, token=str(token))


class BookingChangeDatesPreviewByTokenView(View):
    def post(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _change_dates_preview(request, booking, token=str(token))


class BookingChangeDatesApplyByTokenView(View):
    def post(self, request, token):
        booking = get_object_or_404(Booking, access_token=token)
        return _change_dates_apply(request, booking, actor_user=None, token=str(token))


# ─────────────────────────────────────────────────────────────────────────────
#  MAGIC LINK — "Mis Reservas" sin cuenta
# ─────────────────────────────────────────────────────────────────────────────

_MAGIC_LINK_RATE_WINDOW    = 10   # minutos
_MAGIC_LINK_RATE_EMAIL_MAX = 3    # max solicitudes por email en esa ventana
_MAGIC_LINK_RATE_IP_MAX    = 10   # max solicitudes por IP en esa ventana


def _get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _build_token_url(request, path):
    """En local (DEBUG=True) usa el host real del request; en producción usa SITE_BASE_URL."""
    if settings.DEBUG:
        return request.build_absolute_uri(path)
    return settings.SITE_BASE_URL.rstrip("/") + path


def _send_magic_link_email(email, token_url):
    html_body = render_to_string("emails/magic_link.html", {"token_url": token_url})
    send_mail(
        subject="Tu enlace de acceso a Mis Reservas — Reyes Estancias",
        message=f"Accede a tus reservas aquí: {token_url}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        html_message=html_body,
        fail_silently=False,
    )


class RequestMagicLinkView(View):
    def get(self, request):
        return render(request, "bookings/request_magic_link.html", {
            "form": RequestMagicLinkForm(),
        })

    def post(self, request):
        success_ctx = {"form": RequestMagicLinkForm(), "sent": True}

        form = RequestMagicLinkForm(request.POST)
        if not form.is_valid():
            return render(request, "bookings/request_magic_link.html", {"form": form})

        email = form.cleaned_data["email"].lower()

        # Limpiar sesión de invitado previa: el usuario está iniciando un acceso
        # para un nuevo email, la sesión anterior no debe persistir.
        request.session.pop("guest_email", None)

        ip    = _get_client_ip(request)

        # Rate limit por IP
        ip_key   = f"magic_link_ip_{ip}"
        ip_count = cache.get(ip_key, 0)
        if ip_count >= _MAGIC_LINK_RATE_IP_MAX:
            return render(request, "bookings/request_magic_link.html", success_ctx)

        # Rate limit por email
        email_key   = f"magic_link_em_{email}"
        email_count = cache.get(email_key, 0)
        if email_count >= _MAGIC_LINK_RATE_EMAIL_MAX:
            return render(request, "bookings/request_magic_link.html", success_ctx)

        # Incrementar contadores antes de procesar (se cuentan aunque no haya reservas)
        ttl = _MAGIC_LINK_RATE_WINDOW * 60
        cache.set(ip_key,    ip_count + 1,    timeout=ttl)
        cache.set(email_key, email_count + 1, timeout=ttl)

        # Solo crear enlace y enviar email si el email tiene reservas
        # (anti-enumeración: la respuesta es idéntica en todos los casos)
        if Booking.objects.filter(guest_email=email).exists():
            expires_at = timezone.now() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
            link = MagicLink.objects.create(email=email, expires_at=expires_at)
            path = reverse("validate_magic_link", kwargs={"token": link.token})
            token_url = _build_token_url(request, path)
            try:
                _send_magic_link_email(email, token_url)
            except Exception:
                logger.exception("Error enviando magic link a %s", email)

        return render(request, "bookings/request_magic_link.html", success_ctx)


class ValidateMagicLinkView(View):
    def get(self, request, token):
        try:
            with transaction.atomic():
                link = MagicLink.objects.select_for_update().get(token=token)
                if not link.is_valid():
                    messages.error(request, "El enlace ha expirado o ya fue utilizado.")
                    return redirect("request_magic_link")
                link.used = True
                link.save(update_fields=["used"])
        except MagicLink.DoesNotExist:
            messages.error(request, "Enlace no válido.")
            return redirect("request_magic_link")

        if request.user.is_authenticated and request.user.email.lower() == link.email.lower():
            # Email coincide con la cuenta: marcar email_verified permanentemente
            request.session.cycle_key()
            request.user.email_verified = True
            request.user.save(update_fields=["email_verified"])
        else:
            if request.user.is_authenticated:
                # El link es para un email distinto al del usuario autenticado.
                # Cerramos sesión para que BookingsList pueda leer guest_email correctamente.
                from django.contrib.auth import logout as auth_logout
                auth_logout(request)  # llama flush() internamente — nuevo session key incluido
            else:
                request.session.cycle_key()
            request.session["guest_email"] = link.email
        return redirect("bookings_list")


class SendVerificationLinkView(View):
    """Envía el magic link de verificación al email del usuario autenticado."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        if getattr(request.user, "email_verified", False):
            return redirect("bookings_list")

        email = request.user.email.lower()
        ip    = _get_client_ip(request)

        ip_key   = f"magic_link_ip_{ip}"
        ip_count = cache.get(ip_key, 0)
        email_key   = f"magic_link_em_{email}"
        email_count = cache.get(email_key, 0)

        ttl = _MAGIC_LINK_RATE_WINDOW * 60
        if ip_count < _MAGIC_LINK_RATE_IP_MAX and email_count < _MAGIC_LINK_RATE_EMAIL_MAX:
            cache.set(ip_key,    ip_count + 1,    timeout=ttl)
            cache.set(email_key, email_count + 1, timeout=ttl)
            expires_at = timezone.now() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
            link = MagicLink.objects.create(email=email, expires_at=expires_at)
            path = reverse("validate_magic_link", kwargs={"token": link.token})
            token_url = _build_token_url(request, path)
            try:
                _send_magic_link_email(email, token_url)
            except Exception:
                logger.exception("Error enviando enlace de verificación a %s", email)

        return redirect(reverse("bookings_list") + "?verification_sent=1")