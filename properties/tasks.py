# properties/tasks.py
from decimal import Decimal

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.tzutils import compose_aware_dt
from properties.models import Property
from properties.utils.ical import fetch_ical_events
import logging

logger = logging.getLogger(__name__)


def _sync_airbnb_bookings_for_property(prop, events):
    """
    Creates, updates, or cancels Booking objects for a property based on iCal events.
    Returns the number of active events processed.
    """
    from bookings.models import Booking

    now = timezone.now()
    current_uids = set()

    with transaction.atomic():
        for event in events:
            uid = event["uid"]
            if not uid:
                continue
            current_uids.add(uid)

            arrival = compose_aware_dt(event["start"], hour=15)
            # iCal DTEND is exclusive (checkout day); 12:00 matches web booking convention
            departure = compose_aware_dt(event["end"], hour=12)
            guest_name = event.get("summary") or "Airbnb"
            phone = event.get("phone", "")
            confirmation_code = event.get("confirmation_code", "")

            Booking.objects.update_or_create(
                property=prop,
                source="airbnb",
                ical_uid=uid,
                defaults={
                    "status": "confirmed",
                    "arrival": arrival,
                    "departure": departure,
                    "guest_name": guest_name,
                    "guest_phone": phone,
                    "airbnb_confirmation_code": confirmation_code,
                    "person_num": 1,
                    "total_amount": Decimal("0.00"),
                    "deposit_amount": Decimal("0.00"),
                    "balance_due": Decimal("0.00"),
                },
            )

        # Cancel future Airbnb bookings that disappeared from the feed
        if current_uids:
            cancelled = (
                Booking.objects
                .filter(property=prop, source="airbnb", status="confirmed", departure__gte=now)
                .exclude(ical_uid__in=current_uids)
                .update(status="cancelled")
            )
            if cancelled:
                logger.info(f"Canceladas {cancelled} reservas de Airbnb eliminadas del iCal para '{prop.name}'")

    return len(current_uids)


@shared_task
def sync_all_property_calendars():
    """
    Syncs iCal calendars for all properties that have airbnb_ical_url configured.

    For each property:
    1. Fetches events from the iCal feed (uses 15-min cache)
    2. Creates/updates Booking objects with source="airbnb"
    3. Cancels future Airbnb bookings no longer in the feed
    """
    logger.info("=" * 70)
    logger.info("Iniciando sincronización automática de calendarios iCal")
    logger.info("=" * 70)

    properties_with_ical = Property.objects.filter(
        airbnb_ical_url__isnull=False
    ).exclude(airbnb_ical_url='')

    total = properties_with_ical.count()
    success = 0
    errors = 0
    total_bookings = 0

    if total == 0:
        logger.warning("No hay propiedades con calendario iCal configurado")
        return {'total': 0, 'success': 0, 'errors': 0, 'total_bookings': 0}

    logger.info(f"Sincronizando {total} propiedades con calendarios iCal…")

    for prop in properties_with_ical:
        try:
            logger.info(f"Sincronizando '{prop.name}' (ID: {prop.id})…")
            events = fetch_ical_events(prop.airbnb_ical_url)
            count = _sync_airbnb_bookings_for_property(prop, events)
            logger.info(f"✅ '{prop.name}': {count} reservas sincronizadas")
            success += 1
            total_bookings += count
        except Exception as e:
            logger.error(
                f"❌ Error sincronizando '{prop.name}' (ID: {prop.id}): {e}",
                exc_info=True,
            )
            errors += 1

    logger.info("=" * 70)
    logger.info(f"Completado: {success}/{total} OK, {errors} errores, {total_bookings} reservas totales")
    logger.info("=" * 70)

    return {
        'total': total,
        'success': success,
        'errors': errors,
        'total_bookings': total_bookings,
    }


@shared_task
def sync_single_property_calendar(property_id):
    """Syncs the iCal calendar for a single property. Useful for on-demand sync."""
    try:
        prop = Property.objects.get(id=property_id)
    except Property.DoesNotExist:
        logger.error(f"Propiedad con ID {property_id} no existe")
        return {'success': False, 'error': 'Property not found', 'property_id': property_id}

    if not prop.airbnb_ical_url:
        logger.warning(f"Propiedad '{prop.name}' no tiene calendario iCal configurado")
        return {'success': False, 'error': 'No iCal URL configured', 'property_id': property_id}

    try:
        events = fetch_ical_events(prop.airbnb_ical_url)
        count = _sync_airbnb_bookings_for_property(prop, events)
        logger.info(f"✅ Calendario sincronizado para '{prop.name}': {count} reservas")
        return {'success': True, 'property_id': property_id, 'property_name': prop.name, 'bookings_count': count}
    except Exception as e:
        logger.error(f"Error sincronizando '{prop.name}' (ID: {property_id}): {e}", exc_info=True)
        return {'success': False, 'error': str(e), 'property_id': property_id}
