# Bug: Reserva fantasma por sincronización circular Airbnb ↔ reyesestancias

**Fecha detectado:** 2026-08-02  
**Severidad:** Alta — bloqueaba el calendario de Airbnb con reservas inexistentes  
**Estado:** Corregido

---

## Síntomas observados

El bug se manifestó en dos ocasiones distintas:

1. **Al crear una nueva propiedad en producción** desde el admin de Django: aparecía automáticamente una reserva que cubría un mes completo, sin que nadie la hubiera creado manualmente.

2. **Aparición espontánea**: el calendario de Airbnb del Departamento Azaleas se bloqueó durante todo agosto por una supuesta reserva "proveniente de reyesestancias", sin que existiera ninguna reserva real en el sistema para esas fechas.

---

## Causa raíz

El sistema tiene dos flujos de sincronización de calendarios:

- **Airbnb → reyesestancias** (`sync_all_property_calendars`, cada 30 min): lee el iCal de Airbnb e importa cada VEVENT como un objeto `Booking(source="airbnb", status="confirmed")`.
- **reyesestancias → Airbnb** (`generate_ical_for_property`): genera un feed iCal con las reservas confirmadas de la propiedad, para que Airbnb bloquee esas fechas.

El bug era que `generate_ical_for_property` **no filtraba por `source`**: incluía en el feed todas las reservas confirmadas, también las que ya venían de Airbnb. Al exportarlas de vuelta, les asignaba un UID nuevo (`booking_X@reyesestancias.com`), diferente al UID original de Airbnb. Airbnb interpretaba ese evento como una reserva nueva proveniente de la web y bloqueaba las fechas indicando "reserva de reyesestancias".

### El bucle completo (escenario 2)

```
Airbnb tiene un bloqueo en agosto
    │
    ▼ (cada 30 min, sync_all_property_calendars)
reyesestancias crea Booking(source="airbnb", status="confirmed", agosto)
    │
    ▼ (Airbnb lee el feed iCal de reyesestancias)
generate_ical_for_property incluye esa reserva
con UID "booking_X@reyesestancias.com"
    │
    ▼
Airbnb ve un evento nuevo con UID desconocido
y lo registra como reserva proveniente de la web
    │
    ▼
El calendario de Airbnb muestra agosto bloqueado
"por una reserva de reyesestancias"
```

### Por qué ocurría al crear una propiedad nueva (escenario 1)

Cuando una propiedad nueva se publica en Airbnb, su iCal suele contener VEVENTs de tipo "Not available" o "Blocked" que cubren periodos largos (el mes completo, o el año) mientras el anfitrión no abre disponibilidad manualmente. El sync de Celery, al ejecutarse dentro de los primeros 30 minutos tras añadir la propiedad en el admin, importaba esos bloques como `Booking(source="airbnb")` de mes completo. Esas reservas luego se re-exportaban a Airbnb como si vinieran de la web, amplificando el problema.

---

## Archivos involucrados

| Archivo | Función | Rol en el bug |
|---|---|---|
| `properties/utils/ical.py` | `generate_ical_for_property` | Exportaba reservas de Airbnb de vuelta a Airbnb |
| `properties/tasks.py` | `_sync_airbnb_bookings_for_property` | Importaba bloques genéricos de Airbnb como reservas reales |
| `reyes_estancias/settings.py` | `CELERY_BEAT_SCHEDULE` | Ejecuta el sync cada 30 min automáticamente |

---

## Solución aplicada

**Archivo:** `properties/utils/ical.py`  
**Función:** `generate_ical_for_property` (línea ~243)

Se añadió `.exclude(source='airbnb')` al queryset de bookings que se exportan en el feed iCal:

```python
# ANTES (bug):
bookings = property_obj.bookings.filter(
    Q(status='confirmed') |
    Q(status='pending', hold_expires_at__gt=current_time)
).order_by('arrival')

# DESPUÉS (fix):
# Exclude Airbnb-sourced bookings: Airbnb already knows about them,
# and re-exporting them back creates a circular sync that makes Airbnb
# show a phantom "reyesestancias" reservation with a new UID.
bookings = property_obj.bookings.filter(
    Q(status='confirmed') |
    Q(status='pending', hold_expires_at__gt=current_time)
).exclude(source='airbnb').order_by('arrival')
```

### Lógica del fix

- Las reservas `source="airbnb"` ya existen en el calendario de Airbnb. No necesitan re-exportarse.
- Las reservas `source="web"` son las que Airbnb desconoce y para las que hay que bloquear fechas.
- Al excluir las de Airbnb del feed exportado, se rompe el bucle circular sin afectar la funcionalidad principal.

---

## Limpieza manual necesaria en producción

Tras aplicar el fix, las reservas fantasma ya existentes en la BD deben cancelarse manualmente para que Airbnb las retire de su calendario la próxima vez que lea el feed.

En el admin de Django → Reservas, filtrar por:
- **Propiedad:** Departamento Azaleas (u otras afectadas)
- **Origen:** Airbnb
- **Estado:** Confirmada
- **Fechas:** agosto 2026 (u otras sospechosas)

Si la reserva no tiene código de confirmación real de Airbnb (campo vacío o nombre de huésped "Airbnb"), cambiar su estado a **Cancelada**.

---

## Qué NO cambia con este fix

- El sync **Airbnb → reyesestancias** sigue funcionando igual: las reservas reales de huéspedes de Airbnb se importan correctamente y bloquean el calendario interno de reyesestancias.
- El feed **reyesestancias → Airbnb** sigue bloqueando fechas para todas las reservas web confirmadas y los holds activos.
- Solo se deja de re-exportar lo que Airbnb ya sabe.

---

## Mejora aplicada: distinción entre bloqueo manual y reserva real en el calendario admin

El sync importa como `Booking` **cualquier** VEVENT del iCal de Airbnb, incluidos bloqueos manuales (sin huésped real) del tipo "Not available" o "Blocked". Estos siguen importándose — son útiles para bloquear el calendario interno — pero ahora se distinguen visualmente de las reservas reales de huéspedes.

El criterio es el campo `airbnb_confirmation_code`: las reservas reales de Airbnb siempre incluyen un código de confirmación extraído de la URL de la descripción (e.g. `HMEZEDPKFQ`); los bloqueos manuales no tienen ese dato.

### Archivos modificados

**`bookings/admin.py`** — `_calendario_view`

```python
# ANTES: todo bloque de Airbnb recibía el mismo tratamiento
is_airbnb = b.source == "airbnb"
css_class    = "airbnb" if is_airbnb else b.status
status_label = "Reserva Airbnb" if is_airbnb else STATUS_LABELS.get(b.status, b.status)
label        = b.guest_name if is_airbnb else ...

# DESPUÉS: se distingue entre bloqueo manual y reserva real
is_airbnb       = b.source == "airbnb"
is_airbnb_block = is_airbnb and not b.airbnb_confirmation_code

if is_airbnb_block:
    css_class    = "airbnb_block"
    status_label = "Bloqueo manual Airbnb"
    label        = "Bloqueo"
elif is_airbnb:
    css_class    = "airbnb"
    status_label = "Reserva cliente (Airbnb)"
    label        = b.guest_name or "Airbnb"
else:
    css_class    = b.status
    status_label = STATUS_LABELS.get(b.status, b.status)
    label        = (b.user.get_full_name() or b.user.email) if b.user else (b.guest_name or b.guest_email or "Invitado")
```

**`bookings/static/bookings/css/styles_calendario.css`**

```css
/* Nueva clase para bloqueos manuales de Airbnb */
.booking-block.airbnb_block  { background: #6b7280; cursor: default; opacity: 0.85; }
```

**`bookings/templates/admin/bookings/booking/calendario.html`** — leyenda actualizada

```
🔴 Reserva cliente (Airbnb)   ← tiene código de confirmación real
⚫ Bloqueo manual Airbnb      ← sin código, gris y semi-transparente
```

### Sin cambios en el modelo ni en el sync

No se requiere migración. El campo `airbnb_confirmation_code` ya existía y ya se rellenaba correctamente desde el iCal. Solo se consume de forma diferente en la capa de presentación.
