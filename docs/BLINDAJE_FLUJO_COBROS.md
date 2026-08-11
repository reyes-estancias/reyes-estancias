# Blindaje del flujo de cobros — Reyes Estancias

**Fecha:** 2026-08-11
**Ámbito:** `payments/services.py`, `payments/views.py`, `payments/tasks.py`, `payments/management/commands/charge_due_balances.py`, `bookings/models.py`

Este documento describe, con detalle, todos los problemas detectados en el flujo de cobros y cómo se han resuelto. El disparador fue un **doble cobro real del balance** observado en Stripe (dos cargos "Cargo del 70%" con la misma fecha y segundos de diferencia).

---

## Contexto: cómo funciona el flujo de cobros

Una reserva se paga en dos fases:

1. **Depósito (30%)** — el huésped paga vía Stripe Checkout. Al confirmarse (`checkout.session.completed`), la reserva pasa a `confirmed` y se **programa** el cobro del balance para `arrival + 1 día` (`reschedule_balance_charge`).
2. **Balance (70%)** — se cobra automáticamente **off-session** (con la tarjeta ya guardada) mediante Celery. Si la tarjeta falla o requiere 3DS, se genera un **Checkout Session** de respaldo y se envía un email con el link para pagar a mano.

Existen **dos mecanismos automáticos** que disparan el cobro del balance:

- **Task ETA** (`reschedule_balance_charge` → `charge_balance_for_booking`), agendada a `arrival + 1 día`.
- **Scan periódico** (`scan_and_charge_balances`), en Celery Beat **cada 15 minutos**, que recoge reservas con check-in hace ≥ 48h y balance pendiente.

El hecho de que **ambos mecanismos convivan** es la raíz de varios de los problemas de concurrencia.

Funciones/piezas clave:

- `ensure_balance_payment(booking, payment_type, amount)` — obtiene o crea el `Payment` del balance.
- `charge_offsession_with_fallback(...)` — intenta el cobro off-session; si falla, crea Checkout Session + email.
- `compute_balance_due_snapshot(booking)` — **fuente de verdad** del saldo pendiente, calculada desde los `Payment` reales.
- `stripe_webhook(...)` — procesa los eventos de Stripe.
- Idempotency key usada en Stripe: `f"{payment_type}-{payment.id}"` (p.ej. `balance-42`).

---

## Resumen de problemas

| # | Problema | Severidad | Tipo |
|---|----------|-----------|------|
| 1 | `ensure_balance_payment` creaba un `Payment` nuevo si el anterior estaba `paid` | Crítica | Doble cobro |
| 2 | El webhook reprogramaba el cobro del balance al pagar **el propio balance** | Crítica | Doble cobro |
| 3 | Race condition: el lock del Booking se liberaba antes de llamar a Stripe | Alta | Doble cobro |
| 4 | El fallback creaba una Checkout Session nueva en cada intento | Media | Doble cobro |
| 5 | `_retry_balance_logic`: crash con `None` y no comprobaba si ya estaba pagado | Media | Doble cobro / crash |
| 6 | Management command roto (importaba una función inexistente) | Baja | Código muerto |
| 7 | Cálculo del saldo duplicado y divergente (`balance_due_runtime`) | Alta | Cálculo incorrecto |
| 8 | `.exclude()` mal construido: excluía reservas que **sí** había que cobrar | Alta | Cobro que no se realiza |
| 9 | `payment_intent.payment_failed` revertía un pago ya `paid` | Alta | Doble cobro |
| 10 | `CheckoutCancelView` cancelaba reservas confirmadas | Alta | Pérdida de reserva |
| 11 (B) | off-session que triunfa con un link de Checkout aún vivo | Baja | Doble cobro (residual) |
| 12 (C) | Uso del campo `booking.balance_due` (posiblemente obsoleto) en el guard | Media | Fragilidad |

---

## Problema 1 — `ensure_balance_payment` creaba un `Payment` nuevo si el anterior ya estaba pagado

**Severidad:** Crítica · **Archivo:** `payments/services.py`

### Qué pasaba

La función solo buscaba pagos en estado `pending`/`requires_action`:

```python
# ANTES
p = (booking.payments
     .select_for_update()
     .filter(payment_type=payment_type, status__in=["pending","requires_action"])
     .order_by("-id").first())
if p:
    ...
    return p
return Payment.objects.create(...)   # crea uno NUEVO si no encuentra pending
```

Si el balance ya estaba cobrado (`status="paid"`), **no lo encontraba** y creaba un `Payment` **nuevo con otro `id`**. Como la idempotency key de Stripe es `balance-{payment.id}`, un `id` distinto → **key distinta** → Stripe lo trata como un cobro totalmente nuevo → **doble cobro**.

### Cómo se arregló

Se añade un guard que, **solo para `balance`**, devuelve el pago ya pagado (para que el llamador detecte `already_paid` y no cree uno nuevo). Además se serializa con un lock del Booking (ver Problema 3).

```python
# DESPUÉS
def ensure_balance_payment(booking, payment_type, amount):
    from bookings.models import Booking
    with transaction.atomic():
        # Lock sobre la fila del Booking: serializa llamadas concurrentes (ver Problema 3)
        Booking.objects.select_for_update().filter(pk=booking.pk).first()

        # Solo puede existir UN balance pagado por reserva.
        # OJO: solo para "balance". Extensiones y penalizaciones SÍ pueden repetirse.
        if payment_type == "balance":
            paid = (booking.payments
                    .filter(payment_type="balance", status="paid")
                    .order_by("-id").first())
            if paid:
                return paid

        p = (booking.payments
             .select_for_update()
             .filter(payment_type=payment_type, status__in=["pending","requires_action"])
             .order_by("-id").first())
        if p:
            if p.amount != amount:
                p.amount = amount
                p.save(update_fields=["amount"])
            return p

        return Payment.objects.create(
            booking=booking, payment_type=payment_type,
            status="pending", amount=amount, currency="MXN",
        )
```

**Matiz importante:** el guard se limita a `payment_type == "balance"`. Las **extensiones** de estancia pueden cobrarse varias veces legítimamente (un huésped puede extender dos veces), igual que las penalizaciones; por eso no se les aplica el guard.

---

## Problema 2 — El webhook reprogramaba el cobro del balance al pagar el propio balance

**Severidad:** Crítica · **Archivo:** `payments/views.py` (`stripe_webhook`, evento `checkout.session.completed`)

### Qué pasaba

Tras procesar cualquier `checkout.session.completed`, el código reprogramaba **incondicionalmente** el cobro del balance:

```python
# ANTES
booking.save(update_fields=update)
_booking_sched = booking
transaction.on_commit(
    lambda b=_booking_sched: reschedule_balance_charge(b, b.arrival + timedelta(days=1), settings.SITE_BASE_URL)
)
```

Escenario del doble cobro:

1. Depósito pagado → se programa el cobro del balance.
2. Llega el día → off-session falla (3DS) → se crea Checkout Session + email.
3. El huésped paga el balance por el link.
4. Stripe dispara `checkout.session.completed` → el pago se marca `paid`…
5. …y **se reprograma OTRO cobro de balance** para `arrival + 1 día` (fecha ya pasada → Celery lo ejecuta de inmediato).
6. Ese nuevo cobro creaba otro `Payment` (Problema 1) → **segundo cargo**.

### Cómo se arregló

Solo se reprograma el cobro del balance cuando lo que se acaba de pagar **no es** el propio balance:

```python
# DESPUÉS
booking.save(update_fields=update)

# Solo reprogramar cuando el pago NO es el balance (si no, sería un doble cobro)
if payment.payment_type != "balance":
    _booking_sched = booking
    transaction.on_commit(
        lambda b=_booking_sched: reschedule_balance_charge(b, b.arrival + timedelta(days=1), settings.SITE_BASE_URL)
    )
```

Así, `reschedule_balance_charge` se agenda tras el **depósito** (y extensiones/top-ups, que pueden alterar el total), pero **nunca** tras pagar el balance.

---

## Problema 3 — Race condition: el lock se liberaba antes de llamar a Stripe

**Severidad:** Alta · **Archivos:** `payments/tasks.py`, `payments/services.py`

### Qué pasaba

En `charge_balance_for_booking`, el `select_for_update()` sobre el Booking protegía las comprobaciones, pero **se liberaba antes** de llamar a Stripe:

```python
with transaction.atomic():
    b = Booking.objects.select_for_update().get(pk=booking_id)
    ... comprobaciones ...
# ← el lock se libera aquí
result = charge_offsession_with_fallback(...)   # Stripe FUERA del lock
```

Como el **scan** (cada 15 min) y la **task ETA** son mecanismos independientes, dos tareas podían ejecutarse a la vez para la misma reserva, pasar ambas las comprobaciones y llamar a `ensure_balance_payment` en paralelo. Al no existir aún ninguna fila de `Payment`, **ambas creaban una fila distinta** → dos idempotency keys → **doble cobro**.

### Cómo se arregló

Se serializa la creación del `Payment` con un **lock sobre la fila del Booking dentro de `ensure_balance_payment`** (ver código del Problema 1). Al estar todas las llamadas serializadas por el mismo lock:

- La primera crea `P1` (o lo reutiliza).
- La segunda **encuentra `P1`** y lo reutiliza → misma idempotency key `balance-P1` → **Stripe deduplica** el cargo.

Además se reforzó la task con un guard temprano dentro del propio lock:

```python
# payments/tasks.py — dentro de with transaction.atomic() + select_for_update
if Payment.objects.filter(booking=b, payment_type="balance", status="paid").exists():
    logger.info(f"Booking {booking_id} ya tiene balance pagado, omitiendo cobro")
    return "already_paid"
```

---

## Problema 4 — El fallback creaba una Checkout Session nueva en cada intento

**Severidad:** Media · **Archivo:** `payments/services.py` (`charge_offsession_with_fallback`)

### Qué pasaba

Cuando el off-session fallaba (3DS), el código **creaba una nueva Checkout Session y reenviaba email en cada intento**. Como el scan corre cada 15 min, se generaban **múltiples links de pago vivos** simultáneamente para el mismo balance. Si el huésped (o un reintento de Stripe) completaba dos de ellos → **doble cobro**.

### Cómo se arregló

Antes de crear una sesión nueva, se **reutiliza la que ya esté abierta**:

```python
# DESPUÉS — antes de crear una nueva Checkout Session
if payment.stripe_checkout_session_id:
    try:
        existing = stripe.checkout.Session.retrieve(payment.stripe_checkout_session_id)
        if existing.get("status") == "open":
            return {"status": "requires_action", "payment": payment, "checkout_url": existing.url}
    except Exception:
        pass
```

Combinado con la idempotency key estable (`balance-P1`), dentro de las 24h Stripe reproduce el mismo `CardError` en reintentos y no se lanza un nuevo cargo.

---

## Problema 5 — `_retry_balance_logic`: crash con `None` y no comprobaba si ya estaba pagado

**Severidad:** Media · **Archivo:** `payments/views.py`

### Qué pasaba

```python
# ANTES
payment = booking.payments.filter(payment_type="balance").order_by("-created_at").first()
...
if not payment.stripe_checkout_session_id:   # ← crash si payment es None
```

Dos problemas:
- Si no había ningún pago de balance, `payment` era `None` → `AttributeError`.
- No comprobaba si el balance ya estaba **pagado**: al reintentar, podía generar otra Checkout Session para un balance ya cobrado → **doble cobro**.

### Cómo se arregló

```python
# DESPUÉS
payment = booking.payments.filter(payment_type="balance").order_by("-created_at").first()

if payment is None:
    messages.info(request, "No hay ningún cobro de balance pendiente.")
    return redirect("bookings_list")

# Si ya está pagado, no generes otra sesión (evita doble cobro)
if payment.status == "paid" or compute_balance_due_snapshot(booking) <= 0:
    messages.info(request, "El balance ya está pagado.")
    return redirect("bookings_list")
```

---

## Problema 6 — Management command roto

**Severidad:** Baja (código muerto) · **Archivo:** `payments/management/commands/charge_due_balances.py`

### Qué pasaba

El comando importaba una función que **ya no existe** (`charge_balance_offsession_or_send_checkout`) y tenía una firma imposible (`handle(self, request, booking, ...)` — los management commands no reciben `request` ni `booking`). No afectaba al flujo real (que va por Celery), pero **al ejecutarlo fallaba con `ImportError`**. Nadie lo invocaba (no está referenciado en `Procfile`, `railway.json`, `nixpacks.toml`, cron ni Celery Beat), por eso el sistema funcionaba pese a estar roto.

### Cómo se arregló

Reescrito para reutilizar la task ya blindada `charge_balance_for_booking`, con la **misma query** que el scan y opciones útiles de operación:

- `--dry-run` — lista qué reservas se cobrarían, sin cobrar.
- `--async` — encola en Celery (`.delay`).
- (por defecto) — ejecución síncrona con resultado por reserva.

Es una red de seguridad operativa (forzar cobros a mano) que, al usar la misma lógica idempotente, **no puede provocar dobles cobros**.

---

## Problema 7 — Cálculo del saldo duplicado y divergente

**Severidad:** Alta · **Archivos:** `bookings/models.py`, `payments/views.py`

### Qué pasaba

Existían **dos** implementaciones para calcular el saldo pendiente:

- `compute_balance_due_snapshot(booking)` (en `payments/services.py`) — restaba reembolsos de **cualquier** tipo de pago.
- `Booking.balance_due_runtime()` (en `bookings/models.py`) — solo restaba reembolsos de **depósitos**, ignorando reembolsos de balance/extensión.

La consecuencia: `balance_due_runtime` podía **sobreestimar** el saldo y provocar que se **recobrase dinero ya reembolsado**.

### Cómo se arregló

Se eliminó `balance_due_runtime` y se sustituyó por llamadas directas a la fuente única de verdad en los dos únicos llamadores (`payments/views.py`, en `_balance_start_logic` y `_retry_balance_logic`):

```python
# ANTES
amount = booking.balance_due_runtime()
# DESPUÉS
amount = compute_balance_due_snapshot(booking)
```

Ahora hay **una sola** función que calcula el saldo. El campo `booking.balance_due` se mantiene como **cache denormalizado** (necesario para filtrar en BD), siempre reescrito vía `compute_balance_due_snapshot` en el webhook.

---

## Problema 8 — `.exclude()` mal construido: excluía reservas que sí había que cobrar

**Severidad:** Alta · **Archivos:** `payments/tasks.py`, `payments/management/commands/charge_due_balances.py`

### Qué pasaba

Para no re-encolar balances ya pagados, se escribió:

```python
# ANTES
.exclude(payments__payment_type="balance", payments__status="paid")
```

La intención era "excluye reservas con **un pago** que sea balance **Y** esté pagado". Pero Django trata `filter()` y `exclude()` **de forma distinta** al cruzar una relación de muchos: en `exclude()` con varias condiciones sobre una relación multivaluada, **no garantiza que se refieran a la misma fila**. El SQL generado lo confirma:

```sql
-- ROTO: dos EXISTS separados
NOT (
  EXISTS(SELECT 1 FROM payments WHERE payment_type='balance' AND booking_id=X)
  AND
  EXISTS(SELECT 1 FROM payments WHERE status='paid' AND booking_id=X)
)
```

Efecto: una reserva con **depósito pagado** + **balance pendiente** cumple ambos EXISTS (el depósito es `paid`, y existe una fila `balance`) → **queda excluida** → **su balance nunca se cobra por el scan**. Es lo contrario de lo deseado. (No es un doble cobro, sino un **cobro que no se realiza**; el bug se introdujo al añadir el `.exclude()`.)

### Cómo se arregló

Con una **única subconsulta** (`Exists`/`OuterRef`), donde las dos condiciones aplican a la **misma** fila:

```python
# DESPUÉS
from django.db.models import Exists, OuterRef

paid_balance = Payment.objects.filter(
    booking=OuterRef("pk"), payment_type="balance", status="paid"
)
qs = Booking.objects.filter(...).exclude(Exists(paid_balance))
```

```sql
-- CORRECTO: una sola subconsulta
NOT EXISTS(
  SELECT 1 FROM payments
  WHERE booking_id=X AND payment_type='balance' AND status='paid'
)
```

Aplicado tanto en `scan_and_charge_balances` como en el management command.

---

## Problema 9 — `payment_intent.payment_failed` revertía un pago ya pagado

**Severidad:** Alta · **Archivo:** `payments/views.py` (`stripe_webhook`)

### Qué pasaba

El handler ponía `status="requires_action"` **incondicionalmente**:

```python
# ANTES
payment.stripe_payment_intent_id = pi_id
payment.save(update_fields=["stripe_payment_intent_id"])
payment = Payment.objects.filter(stripe_payment_intent_id=pi_id).select_related("booking").first()
if payment:
    payment.status = "requires_action"
    payment.save(update_fields=["status"])
```

Escenario (Stripe **no garantiza el orden** de entrega de webhooks):

1. Off-session falla por 3DS → `PaymentIntent` fallido `PI-1` → se genera evento `payment_intent.payment_failed`.
2. El huésped paga por el link → `PI-2` exitoso → `checkout.session.completed` → el pago se marca **`paid`**.
3. Si el `payment_intent.payment_failed` de `PI-1` llega **después** (retrasado):
   - Sobrescribe `stripe_payment_intent_id` con `PI-1` (el fallido).
   - **Revierte** el pago de `paid` a `requires_action`.
   - En el siguiente scan, el guard `status="paid"` ya no lo ve pagado → **vuelve a cobrar** → **doble cobro**.

### Cómo se arregló

No degradar nunca un pago ya `paid`, ni pisar el `stripe_payment_intent_id` bueno:

```python
# DESPUÉS
elif etype == "payment_intent.payment_failed":
    pi = obj
    pi_id = pi.get("id")
    booking_id = (pi.get("metadata") or {}).get("booking_id")
    payment_id = (pi.get("metadata") or {}).get("payment_id")
    if not (booking_id and payment_id):
        return HttpResponse(status=200)
    try:
        payment = Payment.objects.get(pk=payment_id, booking_id=booking_id)
    except Payment.DoesNotExist:
        return HttpResponse(status=200)
    # No degradar un pago ya cobrado (webhooks desordenados → doble cobro)
    if payment.status == "paid":
        return HttpResponse(status=200)
    payment.stripe_payment_intent_id = pi_id
    payment.status = "requires_action"
    payment.save(update_fields=["stripe_payment_intent_id", "status"])
```

---

## Problema 10 — `CheckoutCancelView` cancelaba reservas confirmadas

**Severidad:** Alta · **Archivo:** `payments/views.py`

### Qué pasaba

La vista de cancelación del Checkout ponía la reserva en `cancelled` **de forma incondicional**:

```python
# ANTES
payment = Payment.objects.filter(booking_id=booking_id).order_by("-created_at").first()
if payment and payment.status == "pending":
    payment.status = "failed"; payment.save(...)
booking = Booking.objects.get(pk=booking_id)
booking.status = "cancelled"       # ← siempre
booking.save(update_fields=["status"])
```

Si un huésped abría el link de pago del **balance** de una reserva ya **confirmada** y pulsaba "cancelar" en Stripe, el `cancel_url` llevaba a esta vista y **cancelaba una reserva ya pagada** solo por echarse atrás en el segundo pago. La lógica mezclaba el cancelar-depósito (sí debe expirar la reserva) con el cancelar-balance (no debe).

### Cómo se arregló

Se distingue por el estado de la reserva: **solo una reserva aún `pending`** (donde el único pago posible es el depósito inicial) puede cancelarse por abandonar el checkout:

```python
# DESPUÉS
class CheckoutCancelView(View):
    def get(self, request):
        booking_id = request.GET.get("booking_id")
        try:
            booking = Booking.objects.get(pk=booking_id)
        except Booking.DoesNotExist:
            messages.info(request, "Operación cancelada")
            return redirect("bookings_list")

        # Solo el depósito inicial (reserva sin confirmar) puede cancelar la reserva.
        # Si ya está confirmada, es un pago SECUNDARIO (balance/extensión/top-up)
        # y NO debe cancelar una reserva ya pagada.
        if booking.status == "pending":
            payment = Payment.objects.filter(booking_id=booking_id).order_by("-created_at").first()
            if payment and payment.status == "pending":
                payment.status = "failed"
                payment.save(update_fields=["status"])
            booking.status = "cancelled"
            booking.save(update_fields=["status"])
            messages.info(request, "Pago fallido, la reserva ha sido cancelada")
        else:
            messages.info(request, "Has cancelado el pago. Tu reserva sigue activa y el saldo continúa pendiente.")
        return redirect("bookings_list")
```

---

## Problema 11 (B) — off-session que triunfa con un link de Checkout aún vivo

**Severidad:** Baja (residual) · **Archivo:** `payments/services.py`, `payments/views.py`

### Qué pasaba

Vector de baja probabilidad: si un fallback 3DS anterior dejó una **Checkout Session abierta** y, más tarde, un cobro **off-session triunfa** (p.ej. cuando la idempotency key ya expiró a las 24h y la tarjeta ya no requiere 3DS), quedaban **dos vías de pago vivas** a la vez. Si el huésped completaba también el link → **doble cobro**.

La dirección inversa (link pagado primero, luego off-session) ya estaba protegida por el guard `paid` de `ensure_balance_payment`.

### Cómo se arregló

Nuevo helper que **expira la Checkout Session abierta** de un pago, invocado siempre que el balance se cobra por **off-session**:

```python
# payments/services.py
def expire_open_checkout_session(payment):
    session_id = getattr(payment, "stripe_checkout_session_id", None)
    if not session_id:
        return
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.get("status") == "open":
            stripe.checkout.Session.expire(session_id)
    except Exception:
        logger.warning("No se pudo expirar la Checkout Session %s del pago %s", session_id, payment.id)
```

Se llama en los dos puntos donde un off-session marca el balance como pagado:

1. **Éxito síncrono** en `charge_offsession_with_fallback`:

```python
if intent.status == "succeeded":
    payment.status = "paid"
    payment.save(update_fields=["stripe_payment_intent_id", "status"])
    expire_open_checkout_session(payment)   # cierra el link vivo
    return {"status": "paid", ...}
```

2. **Webhook `payment_intent.succeeded`** (red de seguridad), vía `on_commit` para no llamar a Stripe dentro de la transacción:

```python
booking.save(update_fields=["balance_due"])
_paid = payment
transaction.on_commit(lambda p=_paid: expire_open_checkout_session(p))
```

Con esto, si el off-session gana, el link queda **expirado** y no puede completarse un segundo cobro.

---

## Problema 12 (C) — Uso del campo `booking.balance_due` (posiblemente obsoleto) en el guard

**Severidad:** Media (fragilidad) · **Archivo:** `payments/services.py`

### Qué pasaba

En `charge_offsession_with_fallback`, el guard "no hay saldo" usaba el **campo denormalizado**:

```python
# ANTES
if not booking.balance_due or booking.balance_due <= 0:
    return {"status": "no_balance", "payment": payment}
```

Si ese campo quedaba desactualizado, podía **saltarse un cobro legítimo** (o no cortar cuando debía). No era un bug activo (el campo se mantiene sincronizado por el webhook), pero sí una fragilidad.

### Cómo se arregló

Se usa la **fuente de verdad** calculada desde los pagos:

```python
# DESPUÉS
if compute_balance_due_snapshot(booking) <= 0:
    return {"status": "no_balance", "payment": payment}
```

Verificado que es seguro para los tres tipos de pago que pasan por esta función:
- **balance**: el snapshot coincide con el `amount` a cobrar.
- **penalización** (no_show / cancellation_fee): cuando la penalización > 0, el saldo neto siempre es > 0 → el snapshot > 0 → pasa.
- **extensión**: `total_amount` ya está actualizado a `T_new` antes de la llamada → snapshot = importe de la extensión > 0 → pasa.

---

## Cobertura final de escenarios

| Escenario | Protección |
|-----------|-----------|
| Cobro normal (happy path) | Un solo cobro; webhooks idempotentes |
| No recobrar si ya está pagado | 4 capas: `balance_due__gt=0` + `Exists(paid_balance)` + guard de la task + guard de `ensure_balance_payment` |
| Tarjeta requiere 3DS / CardError | Fallback + reutiliza sesión abierta + idempotency key estable |
| Pago manual + Celery a la vez | Lock del Booking serializa + idempotency + `already_paid` |
| Race scan + task ETA | Lock del Booking en `ensure_balance_payment` |
| Webhooks duplicados / desordenados | `checkout.session.completed` y `payment_intent.succeeded` idempotentes; `payment_failed` no degrada `paid` |
| off-session gana con link vivo | Se expira la Checkout Session (Problema 11) |
| Campo `balance_due` obsoleto | Se usa el snapshot (Problema 12) |
| Cancelar el checkout del balance | No cancela la reserva confirmada (Problema 10) |
| Cobros que sí deben ejecutarse | `.exclude(Exists(...))` correcto (Problema 8) |

---

## Archivos modificados

| Archivo | Cambios |
|---------|---------|
| `payments/services.py` | `ensure_balance_payment` (lock + guard `paid`); reutilización de Checkout Session; guard con snapshot; helper `expire_open_checkout_session` + llamada en éxito off-session |
| `payments/views.py` | Webhook: no reprograma si `balance`; `payment_failed` no degrada `paid`; `payment_intent.succeeded` expira sesión; guards en `_retry_balance_logic`; `CheckoutCancelView` no cancela reservas confirmadas; uso de snapshot |
| `payments/tasks.py` | Guard `already_paid` en la task; `.exclude(Exists(paid_balance))` en el scan |
| `payments/management/commands/charge_due_balances.py` | Reescrito completo (reutiliza la task; `--dry-run`/`--async`; `Exists`) |
| `bookings/models.py` | Eliminado `balance_due_runtime` (unificado en `compute_balance_due_snapshot`) |

---

## Notas y recomendaciones futuras

- **Idempotency keys de Stripe** caducan a las 24h. El diseño actual es robusto dentro de esa ventana; los guards de estado (`paid`) y el lock del Booking cubren el resto.
- **Doble fuente para el saldo**: se mantiene el campo `booking.balance_due` como cache para poder filtrar en BD, pero el **único cálculo** es `compute_balance_due_snapshot`. Conviene asegurar que cualquier escritura futura del campo pase por esa función.
- **Tests preexistentes que fallan** (ajenos a este trabajo): `tests/test_calendar_sync.py` y `tests/test_eta_and_cancel.py::test_cancel_booking_revoca_eta_y_void_balance` fallan por un `NoReverseMatch` de la URL `cancel_booking`, sin relación con el flujo de cobros.
