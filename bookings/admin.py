import calendar
from datetime import date

from django.contrib import admin, messages
from django.urls import path, reverse
from django.shortcuts import render, redirect
from django.utils import timezone

from properties.models import Property
from .models import Booking, BookingChangeLog


MONTH_NAMES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}
WEEKDAY_ABBR_ES = ["Lu", "Ma", "Mi", "Ju", "Vi", "Sá", "Do"]
STATUS_LABELS = {
    "confirmed": "Confirmada",
    "pending": "Pendiente",
    "completed": "Completada",
    "cancelled": "Cancelada",
    "expired": "Expirada",
}


class AdminBooking(admin.ModelAdmin):
    list_display = ("id", "property__name", "source", "user", "guest_name", "arrival", "departure", "status")
    list_filter = ("property__name", "source", "status", "arrival", "departure")
    readonly_fields = ("hold_expires_at", "source", "ical_uid", "airbnb_confirmation_code", "guest_phone_display")

    @admin.display(description="Teléfono huésped")
    def guest_phone_display(self, obj):
        if not obj.guest_phone:
            return "—"
        if obj.source == "airbnb":
            return f"{obj.guest_phone} (últimos 4 dígitos — dato limitado de Airbnb)"
        return obj.guest_phone
    change_list_template = "admin/bookings/booking/change_list.html"
    actions = ["action_expire_holds", "action_expire_completed"]

    @admin.action(description="Expirar holds vencidos (pendientes sin pago)")
    def action_expire_holds(self, request, queryset):
        now = timezone.now()
        updated = (
            queryset
            .filter(status="pending", hold_expires_at__isnull=False, hold_expires_at__lt=now)
            .update(status="expired")
        )
        self.message_user(request, f"{updated} reserva(s) marcada(s) como expirada(s).", messages.SUCCESS)

    @admin.action(description="Expirar reservas confirmadas cuya salida ya pasó")
    def action_expire_completed(self, request, queryset):
        now = timezone.now()
        updated = (
            queryset
            .filter(status="confirmed", departure__lt=now)
            .update(status="expired")
        )
        self.message_user(request, f"{updated} reserva(s) confirmada(s) marcada(s) como expirada(s).", messages.SUCCESS)

    def get_urls(self):
        urls = super().get_urls()
        extra = [
            path(
                "calendario/",
                self.admin_site.admin_view(self._calendario_view),
                name="bookings_calendario",
            ),
            path(
                "sync-airbnb/",
                self.admin_site.admin_view(self._sync_airbnb_view),
                name="bookings_sync_airbnb",
            ),
        ]
        return extra + urls

    def _sync_airbnb_view(self, request):
        from properties.tasks import sync_all_property_calendars
        try:
            result = sync_all_property_calendars()
            total = result.get("total_bookings", 0)
            errors = result.get("errors", 0)
            if errors:
                self.message_user(
                    request,
                    f"Sync completado con {errors} error(es). {total} reservas sincronizadas.",
                    messages.WARNING,
                )
            else:
                self.message_user(
                    request,
                    f"Sync de Airbnb completado: {total} reservas sincronizadas.",
                    messages.SUCCESS,
                )
        except Exception as e:
            self.message_user(request, f"Error al sincronizar: {e}", messages.ERROR)

        return redirect(reverse("admin:bookings_calendario") + f"?year={request.GET.get('year', '')}&month={request.GET.get('month', '')}")

    def _calendario_view(self, request):
        today = date.today()

        try:
            year = int(request.GET.get("year", today.year))
            month = int(request.GET.get("month", today.month))
        except (ValueError, TypeError):
            year, month = today.year, today.month

        # Normalizar mes fuera de rango
        if month < 1:
            month, year = 12, year - 1
        elif month > 12:
            month, year = 1, year + 1

        num_days = calendar.monthrange(year, month)[1]
        first_day = date(year, month, 1)
        last_day = date(year, month, num_days)

        # Mes anterior / siguiente para la navegación
        prev_month = month - 1 or 12
        prev_year = year - 1 if month == 1 else year
        next_month = month % 12 + 1
        next_year = year + 1 if month == 12 else year

        # Info de cada día del mes (número, nombre corto, flags)
        days = []
        for d in range(1, num_days + 1):
            dt = date(year, month, d)
            days.append({
                "number": d,
                "weekday": WEEKDAY_ABBR_ES[dt.weekday()],
                "is_weekend": dt.weekday() >= 5,
                "is_today": dt == today,
            })

        # Reservas activas que se solapan con el mes
        bookings_qs = (
            Booking.objects
            .filter(
                status__in=["confirmed", "pending", "completed"],
                arrival__date__lte=last_day,
                departure__date__gte=first_day,
            )
            .select_related("property", "user")
        )

        # Agrupar por propiedad y calcular posición en la pista (%)
        bookings_by_prop: dict[int, list] = {}
        for b in bookings_qs:
            b_start = max(b.arrival.date(), first_day)
            b_end = min(b.departure.date(), last_day)
            if b_end < b_start:
                continue

            col_start = (b_start - first_day).days          # 0-indexed
            duration = (b_end - b_start).days + 1           # inclusive days visible

            left_pct = f"{col_start / num_days * 100:.4f}"
            width_pct = f"{duration / num_days * 100:.4f}"

            is_airbnb = b.source == "airbnb"
            is_airbnb_block = is_airbnb and not b.airbnb_confirmation_code

            if is_airbnb_block:
                css_class = "airbnb_block"
                status_label = "Bloqueo manual Airbnb"
                label = "Bloqueo"
            elif is_airbnb:
                css_class = "airbnb"
                status_label = "Reserva cliente (Airbnb)"
                label = b.guest_name or "Airbnb"
            else:
                css_class = b.status
                status_label = STATUS_LABELS.get(b.status, b.status)
                label = (b.user.get_full_name() or b.user.email) if b.user else (b.guest_name or b.guest_email or "Invitado")

            bookings_by_prop.setdefault(b.property_id, []).append({
                "id": b.id,
                "user": label,
                "status": b.status,
                "css_class": css_class,
                "status_label": status_label,
                "arrival": b.arrival.strftime("%d/%m/%Y"),
                "departure": b.departure.strftime("%d/%m/%Y"),
                "left_pct": left_pct,
                "width_pct": width_pct,
                "url": reverse("admin:bookings_booking_change", args=[b.id]),
            })

        property_rows = [
            {
                "id": prop.id,
                "name": prop.name,
                "bookings": bookings_by_prop.get(prop.id, []),
            }
            for prop in Property.objects.order_by("name")
        ]

        ctx = dict(
            self.admin_site.each_context(request),
            title="Calendario de Reservas",
            opts=Booking._meta,
            year=year,
            month=month,
            month_name=MONTH_NAMES_ES[month],
            prev_year=prev_year,
            prev_month=prev_month,
            prev_month_name=MONTH_NAMES_ES[prev_month],
            next_year=next_year,
            next_month=next_month,
            next_month_name=MONTH_NAMES_ES[next_month],
            days=days,
            num_days=num_days,
            property_rows=property_rows,
        )
        return render(request, "admin/bookings/booking/calendario.html", ctx)


class AdminBookingChangeLog(admin.ModelAdmin):
    list_display = ("id", "booking__property__name", "actor", "new_arrival", "new_departure")
    list_filter = ("actor", "new_arrival", "new_departure")
    readonly_fields = ("created_at",)


admin.site.register(Booking, AdminBooking)
admin.site.register(BookingChangeLog, AdminBookingChangeLog)
