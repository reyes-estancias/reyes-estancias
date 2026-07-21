# Sincronización de Calendarios — Reyes Estancias ↔ Airbnb

## Resumen del sistema

La sincronización es **bidireccional**:

| Dirección | Qué hace | Cómo |
|---|---|---|
| Airbnb → esta web | Crea/actualiza `Booking` objects con `source="airbnb"` | Fetch periódico del iCal de Airbnb vía Celery |
| Esta web → Airbnb | Bloquea en Airbnb las fechas reservadas aquí | Endpoint `.ics` público que Airbnb consulta periódicamente |

---

## Dirección 1: Airbnb → esta web

### Flujo completo paso a paso

```
Airbnb iCal feed (.ics)
        │
        ▼
properties/utils/ical.py
  fetch_ical_events(url)
  ├── Valida URL contra whitelist de hosts permitidos
  ├── Consulta caché Redis (TTL: 15 min)
  │     ├── HIT  → devuelve eventos cacheados
  │     └── MISS → hace request HTTP y cachea resultado
  ├── Parsea el iCal: extrae DTSTART, DTEND, UID, SUMMARY, DESCRIPTION
  └── Devuelve lista de eventos con: uid, start, end, summary, phone, confirmation_code
        │
        ▼
properties/tasks.py
  _sync_airbnb_bookings_for_property(prop, events)
  ├── Por cada evento: Booking.objects.update_or_create(
  │     lookup: property + source="airbnb" + ical_uid
  │     defaults: arrival, departure, guest_name, guest_phone,
  │               airbnb_confirmation_code, status="confirmed"
  │   )
  └── Cancela reservas futuras que desaparecieron del feed (status → "cancelled")
        │
        ▼
Booking objects en base de datos (source="airbnb")
  → Visibles en Admin → Reservas (listado)
  → Visibles en Admin → Calendario de Reservas
```

### Cuándo se ejecuta

| Trigger | Frecuencia |
|---|---|
| Celery Beat automático | Cada 30 minutos |
| Botón "↻ Sincronizar Airbnb" en el calendario del admin | Bajo demanda (inmediato) |
| Shell manual | Bajo demanda |

El botón del admin ejecuta la tarea **de forma síncrona** (sin pasar por Celery), por lo que funciona aunque Celery no esté corriendo (útil en local y para forzar una actualización urgente en producción).

### Qué datos llegan desde Airbnb

Airbnb limita intencionalmente la información disponible en el feed iCal por privacidad. Este es el contenido real de cada evento:

```
DTSTART:20260801
DTEND:20260805
SUMMARY:Reserved
UID:1418fb94e984-ff5c2b1ea70fb671e2b82dec74c7919d@airbnb.com
DESCRIPTION:Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMEZEDPKFQ
             Phone Number (Last 4 Digits): 4388
```

| Campo iCal | Contenido real | Dónde se guarda en `Booking` |
|---|---|---|
| `DTSTART` | Fecha de check-in | `arrival` (hora 15:00 aplicada) |
| `DTEND` | Fecha de check-out (exclusiva, día siguiente) | `departure` (hora 12:00 aplicada) |
| `UID` | Identificador único del evento | `ical_uid` (usado para deduplicación) |
| `SUMMARY` | `"Reserved"` (siempre genérico) | Descartado; `guest_name` queda como `"Airbnb"` |
| `DESCRIPTION` → URL | Código de confirmación (ej. `HMEZEDPKFQ`) | `airbnb_confirmation_code` |
| `DESCRIPTION` → Phone | Últimos 4 dígitos del teléfono (ej. `4388`) | `guest_phone` |

**Lo que Airbnb NO envía por iCal** (solo accesible desde su panel web o API oficial):
- Nombre completo del huésped
- Email del huésped
- Teléfono completo
- Precio / monto de la reserva
- Número de personas

### Campos del modelo `Booking` para reservas de Airbnb

```python
source = "airbnb"                         # identifica el origen
ical_uid = "abc123@airbnb.com"            # UID del evento iCal (para deduplicación)
airbnb_confirmation_code = "HMEZEDPKFQ"  # código visible en el panel de Airbnb
guest_name = "Airbnb"                     # no hay nombre real disponible via iCal
guest_phone = "4388"                      # últimos 4 dígitos
arrival = datetime(2026-08-01 15:00)      # check-in a las 15:00
departure = datetime(2026-08-05 12:00)    # check-out a las 12:00
status = "confirmed"
person_num = 1                            # dato no disponible, valor por defecto
total_amount = 0.00                       # dato no disponible via iCal
```

### Lógica de deduplicación

La clave única para un evento de Airbnb es la combinación `(property, source="airbnb", ical_uid)`. Si al sincronizar un evento ya existe, se actualiza con los datos más recientes (fechas, etc.). No se crean duplicados.

### Cancelaciones automáticas

Si una reserva con `source="airbnb"` y `departure >= ahora` ya no aparece en el feed, la tarea la marca automáticamente como `cancelled`. Esto cubre el caso de cancelaciones en Airbnb.

### Cómo configurar la URL del iCal en el Admin

1. En el Admin de Django → **Propiedades** → editar propiedad.
2. Campo **"Calendario iCal de Airbnb"**: pegar la URL del iCal de Airbnb.

Para obtener la URL en Airbnb:
> **Airbnb** → Calendario → Disponibilidad → Conectar a otro calendario → **Exportar calendario** → Copiar enlace.

Hosts permitidos (`ICAL_ALLOWED_HOSTS`): `airbnb.com`, `airbnb.es`, `airbnb.mx`, `calendar.google.com`, `booking.com`, `vrbo.com`, `homeaway.com`.

### Archivos clave

| Archivo | Función |
|---|---|
| `properties/utils/ical.py` → `fetch_ical_events()` | Descarga, cachea y parsea el iCal; extrae todos los campos |
| `properties/utils/ical.py` → `_parse_description()` | Extrae código de confirmación y teléfono del campo DESCRIPTION |
| `properties/utils/ical.py` → `_parse_summary()` | Filtra summaries genéricos ("Reserved", "Blocked"…) |
| `properties/tasks.py` → `sync_all_property_calendars()` | Tarea Celery: itera propiedades, llama al sync individual |
| `properties/tasks.py` → `_sync_airbnb_bookings_for_property()` | Crea/actualiza/cancela `Booking` objects |
| `bookings/admin.py` → `_sync_airbnb_view()` | Vista del botón de sync manual en el calendario admin |
| `bookings/models.py` → `Booking` | Modelo con campos `source`, `ical_uid`, `airbnb_confirmation_code` |
| `reyes_estancias/settings.py` → `CELERY_BEAT_SCHEDULE` | Frecuencia del sync automático (cada 30 min) |

---

## Dirección 2: esta web → Airbnb

### Cómo funciona

1. Cada propiedad tiene un `ical_token` único (generado automáticamente al crear la propiedad).
2. El endpoint `/properties/calendar/<ical_token>/` genera y sirve un archivo `.ics` con todas las reservas activas.
3. Airbnb descarga ese `.ics` periódicamente (normalmente cada 3-24 horas) y bloquea esas fechas en su calendario.

### Reservas incluidas en el `.ics` exportado

| Estado | ¿Se exporta? | Condición |
|---|---|---|
| `confirmed` | ✅ Siempre | — |
| `pending` | ✅ Sí | Solo si `hold_expires_at > ahora` (hold activo) |
| `pending` expirado | ❌ No | El hold ha caducado |
| `cancelled` / `expired` | ❌ No | — |
| `completed` | ❌ No | — |

> Las `pending` con hold activo se exportan para evitar dobles reservas mientras el huésped tiene tiempo de pagar el depósito.

### Cómo configurar en Airbnb

1. En el Admin de Django → **Propiedades** → editar la propiedad.
2. Copiar la URL de exportación que aparece en el bloque **"Sincronización con Airbnb"**.
3. Ir a **Airbnb** → Calendario → Disponibilidad → Conectar a otro calendario → **Importar calendario**.
4. Pegar la URL y confirmar.

> El `ical_token` nunca cambia, así que la URL es permanente. No hace falta reconfigurarlo en Airbnb al redesplegar la web.

### Archivos clave

| Archivo | Función |
|---|---|
| `properties/utils/ical.py` → `generate_ical_for_property()` | Genera el `.ics` con las reservas activas |
| `properties/views.py` → `ExportCalendarView` | Sirve el `.ics` en la URL pública |
| `properties/urls.py` | Ruta: `calendar/<str:ical_token>/` |

### Seguridad del endpoint de exportación

- La URL incluye un token de 48 bytes aleatorio (`ical_token`) generado con `secrets.token_urlsafe(48)`.
- Rate limiting: máximo 20 peticiones/hora por IP para evitar enumeración de tokens.
- No requiere autenticación (necesario para que Airbnb acceda sin sesión).

---

## Tiempos de propagación

| Evento | Visible en esta web | Visible en Airbnb |
|---|---|---|
| Reserva en Airbnb | ≤ 30 min (Celery) o inmediato (botón sync) | Inmediato |
| Reserva en esta web (`confirmed`) | Inmediato | ≤ 3-24 h (Airbnb polling) |
| Reserva en esta web (`pending`+hold) | Inmediato | ≤ 3-24 h (Airbnb polling) |
| Cancelación en esta web | Inmediato | ≤ 3-24 h (Airbnb polling) |
| Cancelación en Airbnb | ≤ 30 min (próximo sync) | Inmediato |

---

## Configuración técnica

```python
# reyes_estancias/settings.py

ICAL_REQUEST_TIMEOUT = 10        # segundos para el fetch del iCal externo
ICAL_MAX_SIZE = 5 * 1024 * 1024  # 5 MB máximo por archivo iCal
ICAL_CACHE_TIMEOUT = 900         # 15 minutos de TTL en caché (Redis)

CELERY_BEAT_SCHEDULE = {
    "sync-property-calendars-every-30-min": {
        "task": "properties.tasks.sync_all_property_calendars",
        "schedule": crontab(minute="*/30"),
    },
    ...
}
```

---

## Forzar sincronización manual

**Opción 1 — Botón en el admin** (recomendado):

En el calendario del admin aparece el botón **"↻ Sincronizar Airbnb"** en la esquina superior derecha. Al pulsarlo se sincronizan todas las propiedades de forma inmediata y se muestra un mensaje con el resultado.

**Opción 2 — Shell** (local o Railway):

```bash
python manage.py shell -c "
from properties.tasks import sync_all_property_calendars
print(sync_all_property_calendars())
"
```

**Opción 3 — Propiedad individual**:

```bash
python manage.py shell -c "
from properties.tasks import sync_single_property_calendar
print(sync_single_property_calendar(property_id=1))
"
```

---

## Visibilidad en el Admin

Las reservas de Airbnb sincronizadas son visibles en dos lugares:

**Listado de reservas** (`/admin/bookings/booking/`):
- Columna **Origen** muestra `Airbnb`
- Filtros laterales permiten filtrar por `source=airbnb`
- Los campos `ical_uid` y `airbnb_confirmation_code` son de solo lectura

**Calendario de reservas** (`/admin/bookings/booking/calendario/`):
- Las reservas de Airbnb aparecen en rojo con la etiqueta **"Reserva Airbnb"**
- El nombre mostrado en el bloque es `guest_name` (actualmente `"Airbnb"` ya que el iCal no incluye el nombre real)
- El tooltip muestra fechas de entrada y salida

Para localizar una reserva en el panel de Airbnb usar el `airbnb_confirmation_code` (ej. `HMEZEDPKFQ`).
