# properties/utils/ical.py
import re
import requests
import hashlib
import logging
from datetime import datetime, timedelta, date
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.utils.timezone import make_aware, now, is_aware
from icalendar import Calendar, Event

logger = logging.getLogger(__name__)

ICAL_REQUEST_TIMEOUT = getattr(settings, 'ICAL_REQUEST_TIMEOUT', 10)
ICAL_MAX_SIZE = getattr(settings, 'ICAL_MAX_SIZE', 5 * 1024 * 1024)  # 5 MB
ICAL_CACHE_TIMEOUT = getattr(settings, 'ICAL_CACHE_TIMEOUT', 900)    # 15 minutos
ICAL_ALLOWED_HOSTS = getattr(settings, 'ICAL_ALLOWED_HOSTS', [
    'airbnb.com',
    'airbnb.es',
    'airbnb.mx',
    'calendar.google.com',
    'booking.com',
    'vrbo.com',
    'homeaway.com',
])


def _validate_url(ical_url):
    """Validates the URL scheme and host whitelist. Returns parsed URL or raises ValueError."""
    try:
        parsed = urlparse(ical_url)
    except Exception as e:
        raise ValueError(f"Formato de URL inválido: {e}")

    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f"El esquema de URL debe ser http o https, no '{parsed.scheme}'")

    host = parsed.netloc.lower()
    if not host:
        raise ValueError("URL sin host válido")

    if not any(host == h or host.endswith('.' + h) for h in ICAL_ALLOWED_HOSTS):
        raise ValueError(
            f"El host '{host}' no está en la lista de proveedores permitidos. "
            f"Hosts permitidos: {', '.join(ICAL_ALLOWED_HOSTS)}"
        )

    return parsed


def _fetch_raw_content(ical_url, host):
    """Makes the HTTP request and returns raw bytes. Raises on any error."""
    try:
        response = requests.get(
            ical_url,
            timeout=ICAL_REQUEST_TIMEOUT,
            stream=True,
            headers={
                'User-Agent': 'ReyesEstancias/1.0 (Calendar Sync)',
                'Accept': 'text/calendar, application/octet-stream, */*',
            },
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        raise ValueError(f"Timeout al obtener el calendario de {host} (>{ICAL_REQUEST_TIMEOUT}s)")
    except requests.exceptions.ConnectionError as e:
        raise ValueError(f"Error de conexión al obtener el calendario de {host}")
    except requests.exceptions.HTTPError as e:
        raise ValueError(f"Error HTTP {e.response.status_code} al obtener el calendario")
    except requests.exceptions.RequestException as e:
        raise ValueError(f"Error al obtener el calendario: {e}")

    content_length = response.headers.get('content-length')
    if content_length and int(content_length) > ICAL_MAX_SIZE:
        raise ValueError(
            f"El archivo de calendario es demasiado grande "
            f"({int(content_length) / 1024 / 1024:.1f} MB, máximo {ICAL_MAX_SIZE / 1024 / 1024} MB)"
        )

    try:
        content = b''
        for chunk in response.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > ICAL_MAX_SIZE:
                raise ValueError(
                    f"El archivo de calendario excede el tamaño máximo permitido "
                    f"({ICAL_MAX_SIZE / 1024 / 1024} MB)"
                )
    finally:
        response.close()

    return content


_GENERIC_SUMMARIES = {
    "reserved", "reservado", "reservée", "réservé",
    "airbnb", "not available", "no disponible", "blocked", "bloqueado",
}

def _parse_summary(raw: str) -> str:
    """Returns the summary if it looks like a real name, empty string otherwise."""
    clean = raw.strip()
    if clean.lower() in _GENERIC_SUMMARIES:
        return ""
    return clean[:200]


def _parse_description(raw: str) -> dict:
    """
    Extracts phone digits and confirmation code from Airbnb's DESCRIPTION field.

    Airbnb actual format:
        Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMEZEDPKFQ
        Phone Number (Last 4 Digits): 4388
    """
    result = {"phone": "", "confirmation_code": ""}
    if not raw:
        return result

    # Confirmation code is the last path segment of the reservation URL
    url_match = re.search(r'/reservations/details/([A-Z0-9]{6,20})', raw)
    if url_match:
        result["confirmation_code"] = url_match.group(1)

    # Phone: Airbnb only sends last 4 digits
    phone_match = re.search(
        r'(?:phone|tel[eé]fono?)[^:]*:\s*([\d\s\-().+]{4,20})',
        raw, re.IGNORECASE,
    )
    if phone_match:
        result["phone"] = phone_match.group(1).strip()

    return result


def _to_date(value):
    """Converts iCal dtstart/dtend value to a plain date."""
    if isinstance(value, datetime):
        if not is_aware(value):
            value = make_aware(value)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def fetch_ical_events(ical_url):
    """
    Fetches and parses an iCal feed.

    Returns a list of dicts: {uid, start (date), end (date), summary (str)}
    Results are cached for ICAL_CACHE_TIMEOUT seconds.

    Raises:
        ValueError: on invalid URL, disallowed host, HTTP error, or parse error
    """
    cache_key = f'ical_events:{hashlib.sha256(ical_url.encode()).hexdigest()}'

    cached = cache.get(cache_key)
    if cached is not None:
        logger.info(f"iCal cache HIT for {urlparse(ical_url).netloc}")
        return cached

    parsed = _validate_url(ical_url)
    host = parsed.netloc.lower()

    logger.info(f"iCal cache MISS for {host} — fetching…")
    content = _fetch_raw_content(ical_url, host)

    try:
        cal = Calendar.from_ical(content)
    except Exception as e:
        raise ValueError(f"Error al parsear el calendario: formato inválido")

    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        try:
            start = _to_date(component.get("dtstart").dt)
            end = _to_date(component.get("dtend").dt)
            if start is None or end is None:
                continue
            uid = str(component.get("uid", "")).strip()
            summary = _parse_summary(str(component.get("summary", "")))
            description = _parse_description(str(component.get("description", "")))
            events.append({
                "uid": uid,
                "start": start,
                "end": end,
                "summary": summary,
                "phone": description["phone"],
                "confirmation_code": description["confirmation_code"],
            })
        except Exception as e:
            logger.warning(f"Error parsing iCal event from {host}: {e}")
            continue

    logger.info(f"Fetched {len(events)} events from {host}")
    cache.set(cache_key, events, ICAL_CACHE_TIMEOUT)
    return events


def fetch_ical_bookings(ical_url):
    """
    Backward-compatible wrapper around fetch_ical_events.
    Returns list of (start_date, end_date) tuples.
    """
    return [(e["start"], e["end"]) for e in fetch_ical_events(ical_url)]


def get_blocked_dates(ical_url):
    ranges = fetch_ical_bookings(ical_url)
    blocked = set()
    for start, end in ranges:
        current = start
        while current < end:
            blocked.add(current)
            current += timedelta(days=1)
    return sorted(blocked)


def generate_ical_for_property(property_obj):
    """
    Generates an iCal calendar with confirmed and active-hold bookings
    for a property, so Airbnb can block those dates.
    """
    from django.db.models import Q

    cal = Calendar()
    cal.add('prodid', '-//Reyes Estancias//Calendario de Reservas//ES')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', f'Reservas - {property_obj.name}')
    cal.add('x-wr-timezone', 'America/Mexico_City')

    current_time = now()

    bookings = property_obj.bookings.filter(
        Q(status='confirmed') |
        Q(status='pending', hold_expires_at__gt=current_time)
    ).order_by('arrival')

    for booking in bookings:
        event = Event()
        event.add('dtstart', booking.arrival)
        event.add('dtend', booking.departure)
        event.add('dtstamp', current_time)
        event.add('summary', 'Reservado')
        event.add('uid', f'booking_{booking.id}@reyesestancias.com')
        event.add('status', 'CONFIRMED')
        event.add('transp', 'OPAQUE')
        event.add('description', f'Reserva para {booking.person_num} persona(s)')
        cal.add_component(event)

    return cal
