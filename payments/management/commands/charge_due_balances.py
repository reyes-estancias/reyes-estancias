from django.core.management.base import BaseCommand
from django.utils.timezone import now
from datetime import timedelta
from django.db.models import Exists, OuterRef

from bookings.models import Booking
from payments.models import Payment
from payments.tasks import charge_balance_for_booking


class Command(BaseCommand):
    help = (
        "Cobra el 70% (balance) off-session de las reservas cuyo check-in fue hace >= 48h. "
        "Reutiliza la misma lógica idempotente que el scan periódico de Celery."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            type=str,
            default="http://127.0.0.1:8000",
            help="Base URL para construir success/cancel URLs en los emails (producción: https://tu-dominio.com)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Muestra qué reservas se cobrarían sin ejecutar cargos.",
        )
        parser.add_argument(
            "--async",
            action="store_true",
            dest="use_async",
            help="Encola las tareas en Celery (.delay) en vez de ejecutarlas en el proceso actual.",
        )

    def _candidates(self):
        cutoff = now() - timedelta(hours=48)
        # Única subconsulta: excluye solo si existe un Payment que sea balance Y esté pagado.
        paid_balance = Payment.objects.filter(
            booking=OuterRef("pk"), payment_type="balance", status="paid"
        )
        return (
            Booking.objects
            .filter(
                status="confirmed",
                balance_due__gt=0,
                arrival__lte=cutoff,
                stripe_customer_id__isnull=False,
                stripe_payment_method_id__isnull=False,
            )
            .exclude(Exists(paid_balance))
        )

    def handle(self, *args, **opts):
        base_url = opts["base_url"].rstrip("/")
        dry = opts["dry_run"]
        use_async = opts["use_async"]

        qs = self._candidates()
        count = qs.count()
        self.stdout.write(self.style.NOTICE(f"Encontradas {count} reservas candidatas para cobro del balance."))

        processed = 0
        for b in qs.iterator():
            processed += 1
            self.stdout.write(f"- Booking #{b.id} · saldo pendiente: {b.balance_due} MXN")

            if dry:
                continue

            if use_async:
                charge_balance_for_booking.delay(b.id, base_url)
                self.stdout.write(self.style.SUCCESS(f"  Encolada tarea de cobro (booking #{b.id})"))
                continue

            # Ejecución síncrona en el proceso actual para ver el resultado directamente.
            result = charge_balance_for_booking.apply(args=[b.id, base_url]).result
            style = self.style.SUCCESS if result in ("succeeded", "already_paid", "no_balance") else self.style.WARNING
            self.stdout.write(style(f"  Resultado (booking #{b.id}): {result}"))

        verb = "listadas" if dry else ("encoladas" if use_async else "procesadas")
        self.stdout.write(self.style.SUCCESS(f"Proceso terminado. {processed} reservas {verb}."))
