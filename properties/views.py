from django.shortcuts import render
from django.views.generic.list import ListView
from django.views.generic.detail import DetailView
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.http import HttpResponse, Http404
from django.views import View
from .models import Property, PropertyImage
from .forms import BookingForm
from bookings.models import Booking
from django.db.models import Prefetch
from core.tzutils import compose_aware_dt
from django.utils import timezone
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from properties.utils.ical import fetch_ical_bookings, generate_ical_for_property
import json
from django.utils.safestring import mark_safe
import logging
from django_ratelimit.decorators import ratelimit
from django.utils.decorators import method_decorator

# Configurar logger
logger = logging.getLogger(__name__)
# Create your views here.

class PropertiesList(ListView):
    model = Property
    template_name = "properties/property_list.html"
    context_object_name = "property_list"

    def get_queryset(self):
        cover_prefetch = Prefetch(
            "images",
            queryset=PropertyImage.objects.filter(cover=True)
                     .only("id", "image", "property"),
            to_attr="cover_list",
        )
        all_prefetch = Prefetch(
            "images",
            queryset=PropertyImage.objects.order_by("position", "id")
                     .only("id", "image", "property"),
            to_attr="all_images",
        )
        return (
            Property.objects.all()
            .prefetch_related(cover_prefetch, all_prefetch)
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        checkin = self.request.GET.get("checkin")
        checkout = self.request.GET.get("checkout")
        cant_personas = self.request.GET.get("cant_personas")
        ctx.update(checkin=checkin, checkout=checkout, cant_personas=cant_personas)

        props = list(ctx["property_list"])

        # Disponibilidad como ya hacías
        if checkin and checkout and cant_personas:
            for p in props:
                p.available = p.is_available(checkin, checkout, cant_personas)
        else:
            for p in props:
                p.available = None

        # Flags por usuario
        user = self.request.user
        for p in props:
            p.user_booking_overlap = False
            p.user_has_other_booking = False
            p.user_has_future_booking = False

        if user.is_authenticated and props:
            prop_ids = [p.id for p in props]
            now = timezone.now()

            # Solo confirmed que no hayan expirado
            ub = list(
                Booking.objects
                .filter(user=user, property_id__in=prop_ids, status="confirmed")
                .exclude(departure__lt=now)  # Excluir reservas pasadas
                .only("property_id", "arrival", "departure")
            )

            if checkin and checkout:
                sel_in = compose_aware_dt(checkin, hour=15, minute=0)
                sel_out = compose_aware_dt(checkout, hour=12, minute=0)

                # Marca solape u “otra reserva futura”
                for b in ub:
                    if (b.arrival < sel_out) and (b.departure > sel_in):
                        # solapa con el rango
                        for p in props:
                            if p.id == b.property_id:
                                p.user_booking_overlap = True
                                break
                    elif b.departure >= now:
                        # es futura pero no solapa
                        for p in props:
                            if p.id == b.property_id:
                                p.user_has_other_booking = True
                                break
            else:
                # Sin rango: solo “reserva futura”
                for b in ub:
                    if b.departure >= now:
                        for p in props:
                            if p.id == b.property_id:
                                p.user_has_future_booking = True
                                break

        return ctx
class PropertyDetail(DetailView):
    model = Property
    template_name = "properties/property_detail.html"
    context_object_name = "property"
    success_url = reverse_lazy("bookings_list")

    def get_queryset(self):
        cover_prefetch = Prefetch(
            "images",
            queryset=PropertyImage.objects.filter(cover=True)
                     .only("id", "image", "property"),
            to_attr="cover_list",
        )
        all_prefetch = Prefetch(
            "images",
            queryset=PropertyImage.objects.order_by("position", "id")
                     .only("id", "image", "property"),
            to_attr="all_images",
        )
        return (
            Property.objects.all()
            .prefetch_related(cover_prefetch, all_prefetch)
        )


    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        #Recogo los parametros de la url
        checkin = self.request.GET.get("checkin")
        checkout = self.request.GET.get("checkout")
        cant_personas = self.request.GET.get("cant_personas")

        context["checkin"] = checkin
        context["checkout"] = checkout
        context["cant_personas"] = cant_personas

        #Localizar la reserva y su pago de depósito por si falla

        context["active_booking"] = None
        context["deposit_payment"] = None

        if self.request.user.is_authenticated:
            booking_id = self.request.GET.get("booking_id")

            base_qs = Booking.objects.filter(user=self.request.user,
            property=self.object).order_by("-id").prefetch_related("payments")

            if booking_id:
                booking = base_qs.filter(pk=booking_id).first()
            else:
                # Solo mostrar reservas activas (no expiradas ni canceladas)
                now = timezone.now()
                booking = (base_qs.filter(status__in=["pending", "confirmed"])
                          .exclude(status="confirmed", departure__lt=now)  # Excluir pasadas
                          .first())
            
            if booking:
                context["active_booking"] = booking
                deposit = (booking.payments.filter(payment_type="deposit").order_by("-id").first())
                context["deposit_payment"] = deposit

        #Caso 1- Viene del botón "Reservar ahora"
        if checkin and checkout and cant_personas:
            available = self.object.is_available(checkin, checkout, cant_personas)
            context["available"] = available
            #Form precargado por si quiere cambiar fechas
            context["form"] = BookingForm(initial={"checkin" : checkin, "checkout" : checkout, "cant_personas" : cant_personas})
            if available:
                try:
                    quote = self.object.quote_total(checkin, checkout)
                    quote["discount_pct"] = int(quote["discount_rate"] * 100)
                    context["quote"] = quote
                    context["deposit_amount"] = (quote["total"] * Decimal("0.30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                except Exception:
                    pass
        else:
            #Caso 2- No ha completado el form del home, mostramos el form
            context["available"] = None
            context["form"] = BookingForm()

        #Config para el calendario sincronizado
        if self.object.airbnb_ical_url:
            try:
                blocked_ranges = fetch_ical_bookings(self.object.airbnb_ical_url)
                # Expandir a días individuales
                blocked_dates = []
                for start, end in blocked_ranges:
                    current = start
                    while current < end:
                        blocked_dates.append(current.isoformat())
                        current += timedelta(days=1)
                # Serializar como JSON de forma segura
                context["blocked_dates"] = mark_safe(json.dumps(blocked_dates))
                context["booked_ranges"] = [
                    f"{start.toordinal()}_{end.toordinal()}" for start, end in blocked_ranges
                ]
            except Exception as e:
                # En caso de error, usar lista vacía serializada
                context["blocked_dates"] = mark_safe(json.dumps([]))
                context["booked_ranges"] = []

        return context
    
    #El usuario completa el formulario:
    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = BookingForm(request.POST)
        #Si es válido
        if form.is_valid():
            checkin = form.cleaned_data["checkin"]
            checkout = form.cleaned_data["checkout"]
            cant_personas = form.cleaned_data["cant_personas"]
            # Redirige a la misma detail con los params en la URL (GET)
            url = f"{reverse('property_detail', args=[self.object.pk])}?checkin={checkin}&checkout={checkout}&cant_personas={cant_personas}#detalle"
            return redirect(url)
        #Si no es válido
        else:
            context = self.get_context_data()
            context["form"] = form
            return self.render_to_response(context)


@method_decorator(ratelimit(key='ip', rate='20/h', method='GET', block=True), name='dispatch')
class ExportCalendarView(View):
    """
    Vista pública para exportar calendario iCal de una propiedad.
    URL: /properties/calendar/<ical_token>/
    No requiere autenticación (para que Airbnb pueda acceder).

    Rate limiting: Máximo 20 peticiones por hora por IP para prevenir
    ataques de enumeración de tokens.
    """

    def get(self, request, ical_token):
        # Obtener IP del cliente para logging
        ip_address = self._get_client_ip(request)

        # Buscar propiedad por token
        try:
            property_obj = Property.objects.get(ical_token=ical_token)

            # Log de acceso exitoso (solo en DEBUG o con nivel INFO)
            logger.info(
                f"iCal calendar accessed successfully: "
                f"property={property_obj.name} (ID: {property_obj.id}), "
                f"token={ical_token[:8]}..., ip={ip_address}"
            )

        except Property.DoesNotExist:
            # Log de intento fallido (IMPORTANTE para detectar ataques)
            logger.warning(
                f"Invalid iCal token attempt: "
                f"token={ical_token[:8]}... (length: {len(ical_token)}), "
                f"ip={ip_address}, "
                f"user_agent={request.META.get('HTTP_USER_AGENT', 'Unknown')[:100]}"
            )
            raise Http404("Calendario no encontrado")

        # Generar calendario
        cal = generate_ical_for_property(property_obj)

        # Serializar a formato .ics
        ical_content = cal.to_ical()

        # Crear response
        response = HttpResponse(ical_content, content_type='text/calendar; charset=utf-8')

        # Nombre de archivo seguro (sanitizar para evitar problemas)
        filename = self._sanitize_filename(property_obj.name)
        response['Content-Disposition'] = f'attachment; filename="{filename}.ics"'

        return response

    def _get_client_ip(self, request):
        """Obtiene la IP real del cliente, considerando proxies."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', 'Unknown')
        return ip

    def _sanitize_filename(self, name):
        """Sanitiza el nombre del archivo para evitar problemas."""
        import re
        # Remover caracteres problemáticos
        name = re.sub(r'[^\w\s-]', '', name)
        # Reemplazar espacios y guiones múltiples por un solo guión bajo
        name = re.sub(r'[-\s]+', '_', name)
        # Limitar longitud
        return name[:50] if name else 'calendario'
