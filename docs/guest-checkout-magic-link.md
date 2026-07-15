# Guest Checkout y Magic Link — Documentación completa

## Índice

1. [Situación antes del cambio](#1-situación-antes-del-cambio)
2. [Motivación y decisiones de diseño](#2-motivación-y-decisiones-de-diseño)
3. [Arquitectura del nuevo sistema](#3-arquitectura-del-nuevo-sistema)
4. [Los 11 pasos de implementación](#4-los-11-pasos-de-implementación)
5. [Estado actual del sistema](#5-estado-actual-del-sistema)
6. [Corrección: URL de producción y flujo autenticado (2026-07-14)](#6-corrección-url-de-producción-y-flujo-autenticado-2026-07-14)
7. [Pendiente y próximos pasos](#7-pendiente-y-próximos-pasos)
8. [Referencia rápida de URLs](#8-referencia-rápida-de-urls)
9. [Guía de pruebas locales](#9-guía-de-pruebas-locales)

---

## 1. Situación antes del cambio

### Modelo de usuario y autenticación

El sistema exigía **login obligatorio** para cualquier acción sobre reservas. El login usaba `username` (nombre de usuario) + contraseña. El formulario de registro pedía: `username`, `email`, `teléfono`, `password`.

```
Registro → usuario + email + teléfono + contraseña
Login    → username + contraseña
```

El modelo `User` heredaba de `AbstractUser` sin modificaciones relevantes: `username` era el campo principal de autenticación (`USERNAME_FIELD = 'username'`).

### Modelo de reservas (Booking)

- `Booking.user` era una **FK obligatoria** (no nula). Sin usuario, no había reserva.
- No existían campos `guest_name`, `guest_email`, `guest_phone`.
- No existía `access_token`.
- Todo el flujo post-reserva (cancelar, cambiar fechas, pagar) requería sesión activa.

### Flujo de reserva

```
Propiedad → "Reservar" → LOGIN OBLIGATORIO → formulario de reserva → pago
```

Si el usuario no tenía cuenta, no podía reservar.

### Emails

Los emails de retry de balance referenciaban `booking.user.email` y `booking.user.first_name`. Si `booking.user` era `None` (imposible antes, pero un riesgo de diseño), el sistema reventaría.

### "Mis Reservas"

`BookingsList` era una vista con `LoginRequiredMixin`. Filtraba por `user=request.user`. Sin login, acceso denegado.

### Vistas de acción (cancelar, cambiar fechas, pagar)

Todas con `LoginRequiredMixin`. El authorization check era `booking.user == request.user`.

---

## 2. Motivación y decisiones de diseño

### Por qué quitar el login obligatorio

En webs de alquiler vacacional, exigir cuenta para reservar **aumenta el abandono**. El cliente quiere reservar rápido; crear una cuenta es fricción innecesaria. La alternativa es el **guest checkout**: el cliente da sus datos (nombre, email, teléfono), paga, y recibe confirmación por email.

### Por qué mantener también el login

El cliente quería conservar ambas opciones:
- Usuarios que ya tienen cuenta o quieren crearla → login normal
- Usuarios que no quieren cuenta → checkout como invitado

### El mecanismo central: `access_token`

En lugar de autorizar acciones mediante sesión de usuario, cada reserva tiene un `UUID` único (`access_token`). Con ese token se puede:
- Pagar el depósito
- Pagar el balance
- Cancelar
- Cambiar fechas

El token va en la URL. El email de confirmación lo contiene. Esto funciona para cualquier usuario (con o sin cuenta) sin necesidad de login.

### "Mis Reservas" sin cuenta: Magic Link

Para ver todas las reservas de un email sin tener cuenta, se implementó un flujo de **magic link**:

```
Usuario entra su email → si hay reservas asociadas → recibe email con link de un solo uso
→ al hacer clic → sesión de invitado → ve sus reservas
```

El link expira en 20 minutos y solo puede usarse una vez (seguridad).

### Vinculación virtual de reservas con cuentas

Si un usuario tiene cuenta Y también ha reservado como invitado con el mismo email, sus reservas de invitado **aparecen automáticamente** en "Mis Reservas" cuando su email está verificado (`email_verified = True`). No se escribe nada en la BD — es una union de queries en tiempo de lectura.

---

## 3. Arquitectura del nuevo sistema

### Nuevos campos en `Booking`

```python
guest_name   = CharField(max_length=200, blank=True, default="")
guest_email  = EmailField(blank=True, default="")
guest_phone  = CharField(max_length=20, blank=True, default="")
access_token = UUIDField(default=uuid.uuid4, unique=True, editable=False)
user         = ForeignKey(User, on_delete=SET_NULL, null=True, blank=True)  # ahora nullable
```

`guest_*` se rellenan siempre, incluso cuando el usuario está autenticado (se copia del formulario). `user` solo se rellena si el usuario estaba logueado al reservar.

### Nuevo modelo `MagicLink`

```python
class MagicLink(models.Model):
    email      = EmailField()
    token      = UUIDField(default=uuid.uuid4, unique=True)
    expires_at = DateTimeField()
    used       = BooleanField(default=False)
    created_at = DateTimeField(auto_now_add=True)

    def is_valid(self):
        return not self.used and timezone.now() < self.expires_at
```

TTL: 20 minutos (`MAGIC_LINK_TTL_MINUTES = 20`).

### Cambios en `User`

```python
class User(AbstractUser):
    username       = CharField(max_length=150, unique=True, blank=True)  # auto-generado
    email          = EmailField(unique=True)
    phone          = CharField(...)
    email_verified = BooleanField(default=False)

    USERNAME_FIELD  = 'email'   # login por email, no por username
    REQUIRED_FIELDS = []

    def save(self, *args, **kwargs):
        if not self.username:
            self.username = self.email[:150]  # auto-generado
        super().save(*args, **kwargs)
```

El login ahora es `email + contraseña`. `username` se genera automáticamente del email para compatibilidad con `AbstractUser`.

### Dos caminos paralelos (Phase 3a → 3b)

Durante el desarrollo se mantuvieron **dos caminos activos**:
- **Sesión de usuario** (`LoginRequired`): para usuarios autenticados
- **Token en URL**: para cualquiera, con o sin cuenta

La Phase 3b eliminó el camino de sesión (innecesario) y dejó solo el de token.

### Diagrama de flujo

```
RESERVAR:
  Propiedad → CreateBookingView (sin login)
    → GuestInfoForm (nombre, email, teléfono)
    → Booking creado con guest_* + access_token
    → redirect payment_start_token/<uuid>/

PAGAR:
  StartCheckoutByTokenView → Stripe Checkout
  → webhook checkout.session.completed
    → booking.status = "confirmed"
    → send_booking_confirmation_email (via transaction.on_commit)
    → email con links cancel_booking_sure_token/<uuid>/ y change_dates/<uuid>/

MIS RESERVAS (con cuenta):
  /bookings/bookings_list/ → BookingsList
    → si authenticated: Q(user=user) | Q(guest_email=user.email) [si email_verified]
    → si no authenticated + guest_email en sesión: Q(guest_email=email)
    → si ninguno: redirect request_magic_link

MIS RESERVAS (sin cuenta):
  /bookings/mis-reservas/ → RequestMagicLinkView
    → email → si hay reservas → MagicLink creado → email con token
    → /bookings/mis-reservas/verificar/<uuid>/ → ValidateMagicLinkView
    → session.cycle_key() + session["guest_email"] = email
    → redirect bookings_list

CANCELAR / CAMBIAR FECHAS (desde email o bookings_list):
  /bookings/cancel/<uuid>/sure/ → CancelBookingSureByTokenView
  /bookings/cancel/<uuid>/      → CancelBookingByTokenView (POST)
  /bookings/change_dates/<uuid>/  → BookingChangeDatesStartByTokenView
  /bookings/change_dates/<uuid>/preview/ → preview
  /bookings/change_dates/<uuid>/apply/   → apply
```

---

## 4. Los 11 pasos de implementación

### Paso 1 — Login por email, registro sin username

**Archivos:** `accounts/models.py`, `registration/forms.py`, `registration/views.py`, `registration/urls.py`, templates de login y signup.

**Cambios:**
- `USERNAME_FIELD = 'email'` en el modelo User → Django autentica por email sin custom backend
- `username` pasa a `blank=True` y se auto-genera desde el email en `save()`
- `email_verified = BooleanField(default=False)` añadido al modelo
- `UserRegistrationForm`: campos `first_name`, `last_name`, `email`, `phone`, `password1`, `password2` (sin username)
- `EmailLoginForm`: campo `email` en lugar de `username`
- `EmailLoginView` y `SignUpView` actualizados
- Templates de login y signup rediseñados
- `registration/urls.py` reescrito (eliminada dependencia de `django.contrib.auth.urls`)
- `reyes_estancias/urls.py`: eliminado `path("accounts/", include("django.contrib.auth.urls"))`
- Migraciones: `0002_user_email_verified.py` y `0003_user_username_blank.py`

**Migraciones MySQL:** MySQL asigna un solo valor de `default` a todas las filas en `ALTER TABLE`. Esto afectó al `access_token` en el paso 2 y se resolvió con una migración de datos específica.

---

### Paso 2 — Campos guest en Booking, access_token, modelo MagicLink

**Archivos:** `bookings/models.py`, migraciones `0014` a `0016` + `0015b`.

**Cambios en `Booking`:**
- Añadidos `guest_name`, `guest_email`, `guest_phone` (blank=True, default="")
- Añadido `access_token = UUIDField(unique=True, default=uuid.uuid4)`
- `user` cambiado a `null=True, blank=True, on_delete=SET_NULL`
- Índice `booking_user_idx` sustituido por `booking_guest_email_idx`

**Nuevo modelo `MagicLink`** con `email`, `token`, `expires_at`, `used`, `created_at` y método `is_valid()`.

**Migraciones en orden:**
- `0014`: schema — añade campos, cambia FK, crea MagicLink
- `0015`: datos — rellena `guest_*` desde `booking.user` en registros existentes
- `0015b`: datos — asigna UUID único a cada booking existente (MySQL bug: todos tenían el mismo UUID)
- `0016`: añade `unique=True` al `access_token`

**Por qué 0015b:** MySQL, al añadir una columna con `default=uuid.uuid4`, calcula el default una sola vez y lo asigna a todas las filas. Resultado: todos los bookings tenían el mismo UUID → la constraint `unique` de `0016` fallaba. La solución fue un bucle en Python que asigna `uuid.uuid4()` individualmente a cada booking antes de aplicar la constraint.

---

### Paso 3 — CreateBookingView sin login (GuestInfoForm)

**Archivos:** `bookings/views.py`, `bookings/forms.py`, `bookings/templates/bookings/create_booking.html`.

**Cambios:**
- `CreateBookingView` deja de heredar `LoginRequiredMixin`
- Añadida `GuestInfoForm` (nombre, apellidos, email, teléfono) — precargada si el usuario está autenticado
- POST: valida `GuestInfoForm`, crea `Booking` con `guest_name/email/phone` siempre, `user` solo si está autenticado
- Lógica `get_or_create` para evitar duplicados: busca por `guest_email + property + arrival + departure + status=pending`
- Redirect final: `payment_start_token` con `booking.access_token`
- Template nuevo de dos columnas: resumen de la reserva (izquierda) + formulario de datos (derecha)

**`bookings/forms.py`:** añadidas `GuestInfoForm` y `RequestMagicLinkForm`.

---

### Paso 4 — Phase 3a: vistas por token para payments, cancelación y cambio de fechas

**Archivos:** `payments/views.py`, `payments/urls.py`, `payments/services.py`, `bookings/views.py`, `bookings/urls.py`, templates de cancel y change_dates.

**payments/services.py:**
- Todos los emails que referenciaban `booking.user.email` → `booking.guest_email`
- `booking.user.first_name` → `booking.guest_name`

**payments/views.py:**
- Extraída lógica a helpers de módulo: `_checkout_logic()`, `_retry_deposit_logic()`, `_balance_start_logic()`, `_retry_balance_logic()`
- Añadidas vistas por token: `StartCheckoutByTokenView`, `RetryDepositByTokenView`, `StartBalanceByTokenView`, `RetryBalanceByTokenView`
- `CheckoutSuccesView` y `CheckoutCancelView`: eliminado `LoginRequired`
- `CheckoutSuccesView`: si el usuario no está autenticado, guarda `guest_email` en sesión para que `BookingsList` funcione inmediatamente después del pago

**bookings/views.py:**
- Helpers compartidos: `_cancel_logic()`, `_change_dates_start()`, `_change_dates_preview()`, `_change_dates_apply()`
- Añadidas: `CancelBookingByTokenView`, `CancelBookingSureByTokenView`, `BookingChangeDatesStartByTokenView`, `BookingChangeDatesPreviewByTokenView`, `BookingChangeDatesApplyByTokenView`

**Templates actualizados:**
- `cancel_booking_sure.html`: `{% if token %}` para usar la URL correcta
- `change_dates_form.html`: action condicional según token o pk
- `change_dates_preview.html`: action y enlace "Editar fechas" condicionales

---

### Paso 5 — Email de confirmación post-pago

**Archivos:** `payments/services.py`, `payments/views.py`, `payments/templates/emails/booking_confirmation.html`.

**Nueva función `send_booking_confirmation_email(booking, base_url=None)`:**
- Construye URLs de gestión con `access_token` (sin login): cancel, cambiar fechas
- Renderiza `emails/booking_confirmation.html`
- Envía a `booking.guest_email`

**En el webhook (`checkout.session.completed`):**
- Captura `was_pending = booking.status != "confirmed"` antes de cambiar el estado
- Solo envía email si `was_pending = True` AND `payment_type == "deposit"` AND no es `deposit_topup`
- Usa `transaction.on_commit(lambda b=booking: send_booking_confirmation_email(b, ...))` para ejecutar después del commit

**Corrección en `retry_balance_payment.html`:** `user.first_name|default:user.username` → `guest_name`.

**Email de confirmación incluye:**
- Propiedad, fechas, personas
- Desglose: total, depósito pagado, saldo pendiente
- Botones "Modificar fechas" y "Cancelar reserva" (por token, sin login)
- Fallback con URLs en texto plano

---

### Paso 6 — Magic Link: RequestMagicLinkView y ValidateMagicLinkView

**Archivos:** `bookings/views.py`, `bookings/urls.py`, `bookings/templates/bookings/request_magic_link.html`, `bookings/templates/emails/magic_link.html`.

**`RequestMagicLinkView` (GET/POST en `/bookings/mis-reservas/`):**

*Rate limiting* (anti-spam):
- Por IP: `cache.set("magic_link_ip_{ip}", count, timeout=600)` — máx. 10 en 10 min
- Por email: `cache.set("magic_link_em_{email}", count, timeout=600)` — máx. 3 en 10 min
- Los contadores se incrementan ANTES de procesar (fail-safe)

*Anti-enumeración:* la respuesta es **idéntica** independientemente de si el email tiene reservas o no. Solo se crea el `MagicLink` y se envía el email si `Booking.objects.filter(guest_email=email).exists()`. Así un atacante no puede saber qué emails tienen reservas.

**`ValidateMagicLinkView` (GET en `/bookings/mis-reservas/verificar/<uuid>/`):**
1. `select_for_update()` dentro de `transaction.atomic()` — atomicidad
2. `link.is_valid()` — comprueba `used=False` y `expires_at > now()`
3. `link.used = True; link.save()` — marcado antes del redirect
4. `request.session.cycle_key()` — prevención de session fixation
5. `request.session["guest_email"] = link.email`
6. Redirect a `bookings_list`

**`ClearGuestSessionView` (POST en `/bookings/mis-reservas/salir/`):**
- Elimina `guest_email` de la sesión
- Redirect a `request_magic_link`

---

### Paso 7 — Verificación de email en el registro + audit log

**Archivos:** `registration/views.py`, `registration/urls.py`, `registration/templates/emails/verify_email.html`.

**Mecanismo:** `django.core.signing.dumps(user.pk, salt="email-verify")` — token firmado con HMAC, válido 24 horas. Es URL-safe (usa base64url) y no requiere tabla adicional en BD.

**`SignUpView.form_valid()`:**
1. Guarda el usuario
2. Llama a `_send_verification_email(user, request)` — captura excepciones con `logger.exception`
3. `messages.info()` informando del email enviado
4. Redirect a login

**`VerifyEmailView` (GET en `/accounts/verify-email/<str:token>/`):**
1. `signing.loads(token, salt=..., max_age=86400)` — valida firma Y expiración en un paso
2. Diferencia `SignatureExpired` vs `BadSignature` (mensajes distintos)
3. Si `email_verified` ya era `True`, no hace nada (idempotente)
4. Al activar: **audit log** — si hay reservas de invitado con ese email, escribe en `logger.info` cuántas reservas quedan vinculadas virtualmente. Sin nueva tabla, solo trazabilidad de soporte.

---

### Paso 8 — Celery Beat: purga de MagicLinks expirados

**Archivos:** `bookings/tasks.py`, `reyes_estancias/settings.py`.

**Task `purge_magic_links()`:**

```python
deleted, _ = MagicLink.objects.filter(
    Q(expires_at__lt=now()) |
    Q(used=True, created_at__lt=now() - timedelta(hours=24))
).delete()
```

- Expirados (sin importar si fueron usados): borrado inmediato
- Usados hace más de 24 h: conservados un día para auditoría de soporte, luego borrados

**Schedule:** `crontab(hour=3, minute=45)` — 3:45 AM, después de las otras tareas de mantenimiento nocturno (3:00 bookings expired, 3:15 bookings completed, 3:45 magic links).

---

### Paso 9 — Criterios de validación antes de Phase 3b

No es un paso de código; es un checklist de salida para producción:

- Mínimo **72 horas** con ambos caminos activos sin incidentes
- **3 reservas completadas** (depósito pagado + confirmación recibida) por el camino con cuenta
- **3 reservas completadas** por el camino de invitado (sin login)
- Al menos **1 cancelación** y **1 cambio de fechas** por cada camino
- Verificar en logs de Railway que `email_verified` + audit log registran correctamente
- Confirmar que el email de confirmación llega y los links del token funcionan

---

### Paso 10 — Phase 3b: eliminación de vistas de sesión

**Archivos:** `bookings/views.py`, `bookings/urls.py`, `payments/views.py`, `payments/urls.py`, `bookings/templates/bookings/bookings_list.html`.

**Eliminados de `bookings/views.py`:**
- `CancelBookingView` (LoginRequired)
- `CancelBookingSureView` (LoginRequired)
- `BookingChangeDatesStartView` (LoginRequired)
- `BookingChangeDatesPreviewView` (LoginRequired)
- `BookingChangeDatesApplyView` (LoginRequired)

**Eliminados de `payments/views.py`:**
- `StartCheckoutView` (LoginRequired)
- `RetryDepositPaymentView` (LoginRequired)
- `StartBalanceCheckoutView` (LoginRequired)
- `RetryBalancePaymentView` (LoginRequired)
- Import de `LoginRequiredMixin` (ya no se usa)

**URLs eliminadas:** todas las rutas con `<int:booking_id>` y `<int:pk>` para acciones de reserva.

**`bookings_list.html` actualizado:** todos los botones de acción apuntan ahora a URLs por token:
- `payment_start_token token=booking.access_token`
- `retry_deposit_token`, `start_balance_token`, `retry_balance_token`
- `cancel_booking_sure_token token=booking.access_token`
- `booking_change_dates_start_token token=booking.access_token`

**`RemakeBookingView` actualizado:**
- Redirect cambiado de `payment_start` a `payment_start_token`
- Al crear la nueva reserva, rellena `guest_name/email/phone` desde la reserva original o desde `request.user`

---

### Paso 11 — BookingsList con lógica dual

**Archivos:** `bookings/views.py`, `bookings/urls.py`, `bookings/templates/bookings/bookings_list.html`.

**`BookingsList`** pasa de `LoginRequiredMixin + ListView` a solo `ListView` con `dispatch` propio:

```python
def dispatch(self, request, *args, **kwargs):
    if not request.user.is_authenticated and not request.session.get("guest_email"):
        return redirect("request_magic_link")
    return super().dispatch(request, *args, **kwargs)
```

**`get_queryset()` — lógica dual:**

```python
if request.user.is_authenticated:
    q = Q(user=request.user)
    if user.email_verified:
        q |= Q(guest_email=request.user.email)  # vinculación virtual
    return base.filter(q).distinct()
else:
    guest_email = request.session.get("guest_email", "")
    return base.filter(guest_email=guest_email)
```

**`get_context_data()` añade:**
- `viewer_email`: email del usuario autenticado o del invitado
- `is_guest_session`: True si es sesión de magic link
- `email_verified`: True si la cuenta tiene email verificado

**Banner en el template — tres estados:**
1. **Invitado (magic link):** "Viendo reservas de [email]" + botón "Cambiar correo" → limpia sesión
2. **Cuenta sin verificar:** aviso ámbar → verificar email para ver reservas de invitado
3. **Cuenta verificada:** confirmación verde → todas las reservas visibles

---

## 5. Estado actual del sistema

### Lo que funciona

| Funcionalidad | Estado |
|---|---|
| Reservar sin cuenta | ✅ Implementado |
| Login por email (sin username) | ✅ Implementado |
| Registro sin username | ✅ Implementado |
| `access_token` en cada reserva | ✅ Implementado |
| Pago del depósito por token (sin login) | ✅ Implementado |
| Pago del balance por token | ✅ Implementado |
| Cancelar por token | ✅ Implementado |
| Cambiar fechas por token | ✅ Implementado |
| Email de confirmación post-pago | ✅ Implementado |
| "Mis Reservas" con magic link | ✅ Implementado |
| Rate limiting en magic link | ✅ Implementado (cache Redis) |
| Session fixation prevention | ✅ Implementado (`cycle_key()`) |
| Verificación de email en registro | ✅ Implementado |
| Vinculación virtual de reservas | ✅ Implementado (`email_verified` gate) |
| Audit log de vinculación | ✅ Implementado (`logger.info`) |
| Purga automática de MagicLinks | ✅ Implementado (Celery Beat 3:45 AM) |
| BookingsList dual (cuenta + invitado) | ✅ Implementado |

### Flujo de datos completo

```
1. Reserva (CreateBookingView)
   → Booking{guest_name, guest_email, guest_phone, access_token, user?}

2. Pago (StartCheckoutByTokenView → Stripe → webhook)
   → booking.status = "confirmed"
   → booking.stripe_customer_id, stripe_payment_method_id guardados
   → send_booking_confirmation_email (via on_commit)
   → CheckoutSuccesView: session["guest_email"] = booking.guest_email

3. Confirmación email
   → Links con access_token: cancel, cambiar fechas

4. Mis Reservas (magic link)
   → RequestMagicLinkView: crea MagicLink, envía email
   → ValidateMagicLinkView: session["guest_email"], cycle_key()
   → BookingsList: filtra por guest_email

5. Mis Reservas (con cuenta)
   → BookingsList: filtra por user + guest_email (si email_verified)

6. Acciones desde Mis Reservas
   → Todos los botones usan token URLs
```

---

## 6. Corrección: URL de producción y flujo autenticado (2026-07-14)

### Errores encontrados

#### Error 1 — URL del magic link apuntaba a localhost

**Síntoma:** El correo del magic link llegaba con una URL del tipo `http://127.0.0.1:8000/bookings/mis-reservas/verificar/<uuid>/`. En producción (Railway), el enlace era inaccesible.

**Causa raíz:** La URL se construía con `request.build_absolute_uri()`:

```python
# bookings/views.py — RequestMagicLinkView.post() — ANTES
token_url = request.build_absolute_uri(
    reverse("validate_magic_link", kwargs={"token": link.token})
)
```

`request.build_absolute_uri()` usa la cabecera `HTTP_HOST` del request entrante. En Railway, detrás del proxy inverso de la plataforma, esa cabecera puede contener el hostname interno (`127.0.0.1:8000`) en lugar del dominio público, a menos que `USE_X_FORWARDED_HOST = True` esté activo y el proxy la reescriba correctamente. El proyecto ya tenía `SITE_BASE_URL` en settings exactamente para este caso, pero no se estaba usando.

---

#### Error 2 — Flujo del magic link roto para usuarios autenticados

**Síntoma:** Un usuario registrado que quería ver sus reservas de invitado veía el banner ámbar *"Verifica tu correo"* con un enlace a `/signup/`. Al hacer clic, se le llevaba al formulario de registro — sin sentido para alguien ya autenticado.

Incluso si el usuario llegaba a pedir el magic link manualmente (vía `/bookings/mis-reservas/`), el link del correo no servía de nada: al validarlo, `ValidateMagicLinkView` solo guardaba `guest_email` en la sesión, pero `BookingsList.get_queryset()` para usuarios autenticados **ignora completamente** `session["guest_email"]`; en su lugar usa el flag `email_verified` del modelo `User`. Resultado: el clic en el link no producía ningún efecto visible para el usuario autenticado.

**Causa raíz:** Dos fallos combinados:

1. El template `bookings_list.html` enlazaba a `signup` en lugar de a un mecanismo de verificación real.
2. `ValidateMagicLinkView` no distinguía entre usuario autenticado e invitado, y siempre escribía en la sesión en lugar de persistir la verificación en la cuenta.

---

### Qué había antes (estado pre-corrección)

```
bookings/views.py
  RequestMagicLinkView.post():
    token_url = request.build_absolute_uri(...)  # ← genera localhost en Railway

  ValidateMagicLinkView.get():
    request.session.cycle_key()
    request.session["guest_email"] = link.email  # ← siempre sesión, incluso si está autenticado
    return redirect("bookings_list")

  # No existía SendVerificationLinkView


bookings/templates/bookings/bookings_list.html
  {% if not email_verified %}
    <a href="{% url 'signup' %}">vuelve a registrarte</a>   ← enlace inútil para autenticados
  {% endif %}


bookings/urls.py
  # Solo estas tres rutas:
  path("mis-reservas/",                             RequestMagicLinkView, ...)
  path("mis-reservas/verificar/<uuid:token>/",      ValidateMagicLinkView, ...)
  path("mis-reservas/salir/",                       ClearGuestSessionView, ...)
```

---

### Cambios aplicados

#### 1. `bookings/views.py` — `RequestMagicLinkView.post()`

```python
# ANTES
token_url = request.build_absolute_uri(
    reverse("validate_magic_link", kwargs={"token": link.token})
)

# DESPUÉS
path = reverse("validate_magic_link", kwargs={"token": link.token})
token_url = settings.SITE_BASE_URL.rstrip("/") + path
```

Ahora usa el dominio de producción definido en la variable de entorno `SITE_BASE_URL` (`https://reyesestancias.com` en Railway).

---

#### 2. `bookings/views.py` — `ValidateMagicLinkView.get()`

```python
# ANTES
request.session.cycle_key()
request.session["guest_email"] = link.email
return redirect("bookings_list")

# DESPUÉS
request.session.cycle_key()
if request.user.is_authenticated and request.user.email.lower() == link.email.lower():
    # Usuario autenticado: verifica el email de forma permanente en la cuenta
    request.user.email_verified = True
    request.user.save(update_fields=["email_verified"])
else:
    # Invitado sin cuenta: acceso temporal vía sesión
    request.session["guest_email"] = link.email
return redirect("bookings_list")
```

**Por qué funciona:** `BookingsList.get_queryset()` para usuarios autenticados hace `Q(user=user) | Q(guest_email=user.email)` solo cuando `user.email_verified = True`. Al marcar el flag en la cuenta, la unión de reservas se activa de forma permanente sin necesidad de ninguna sesión auxiliar.

Para invitados sin cuenta, el comportamiento anterior se mantiene intacto.

---

#### 3. `bookings/views.py` — Nueva vista `SendVerificationLinkView`

Vista POST exclusiva para usuarios autenticados. Envía el magic link al email del usuario sin pedirle que lo introduzca de nuevo.

```python
class SendVerificationLinkView(View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        if getattr(request.user, "email_verified", False):
            return redirect("bookings_list")  # ya verificado, nada que hacer

        email = request.user.email.lower()
        ip    = _get_client_ip(request)

        # Reutiliza el mismo rate limiting que RequestMagicLinkView
        ip_key, ip_count     = f"magic_link_ip_{ip}", cache.get(f"magic_link_ip_{ip}", 0)
        email_key, email_count = f"magic_link_em_{email}", cache.get(f"magic_link_em_{email}", 0)

        ttl = _MAGIC_LINK_RATE_WINDOW * 60
        if ip_count < _MAGIC_LINK_RATE_IP_MAX and email_count < _MAGIC_LINK_RATE_EMAIL_MAX:
            cache.set(ip_key,    ip_count + 1,    timeout=ttl)
            cache.set(email_key, email_count + 1, timeout=ttl)
            expires_at = timezone.now() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
            link = MagicLink.objects.create(email=email, expires_at=expires_at)
            path = reverse("validate_magic_link", kwargs={"token": link.token})
            token_url = settings.SITE_BASE_URL.rstrip("/") + path
            try:
                _send_magic_link_email(email, token_url)
            except Exception:
                logger.exception("Error enviando enlace de verificación a %s", email)

        return redirect(reverse("bookings_list") + "?verification_sent=1")
```

Diferencias clave respecto a `RequestMagicLinkView`:
- No hay formulario: el email viene de `request.user.email`.
- No aplica anti-enumeración (el usuario ya sabe su propio email).
- No comprueba si hay reservas con `guest_email` — el envío se hace siempre (el email puede no tener reservas aún, pero la verificación sigue siendo válida).
- Redirige a `bookings_list?verification_sent=1` en lugar de renderizar una plantilla.

---

#### 4. `bookings/urls.py` — Nueva ruta

```python
path("mis-reservas/verificar-email/", SendVerificationLinkView.as_view(), name="send_verification_link"),
```

---

#### 5. `bookings/templates/bookings/bookings_list.html` — Banner ámbar

```html
<!-- ANTES -->
{% if not email_verified %}
  <div class="... border-amber-200 bg-amber-50 ...">
    <p>
      <strong>Verifica tu correo</strong> para ver también las reservas que hiciste sin cuenta.
      Revisa tu bandeja de entrada o
      <a href="{% url 'signup' %}">vuelve a registrarte</a>.   ← enlace inútil
    </p>
  </div>
{% endif %}

<!-- DESPUÉS -->
{% if not email_verified %}
  {% if request.GET.verification_sent %}
    <!-- Confirmación verde tras enviar el enlace -->
    <div class="... border-emerald-200 bg-emerald-50 ...">
      <p>Te hemos enviado un enlace de verificación a <strong>{{ viewer_email }}</strong>.
         Revisa tu bandeja de entrada (y la carpeta de spam).</p>
    </div>
  {% else %}
    <!-- Banner ámbar con botón real -->
    <div class="... border-amber-200 bg-amber-50 ... flex items-center justify-between">
      <p><strong>Verifica tu correo</strong> para ver también las reservas que hiciste
         sin cuenta con <strong>{{ viewer_email }}</strong>.</p>
      <form method="post" action="{% url 'send_verification_link' %}">
        {% csrf_token %}
        <button type="submit">Enviar enlace</button>
      </form>
    </div>
  {% endif %}
{% endif %}
```

**Por qué query param y no `messages`:** el base template (`core/base.html`) no renderiza el framework de messages de Django. En lugar de modificar el base template (cambio de mayor alcance), se usa `?verification_sent=1` para pasar el estado de un redirect GET a la siguiente render.

---

### Estado después de la corrección

| Escenario | Antes | Después |
|---|---|---|
| URL del magic link en producción | `http://127.0.0.1:8000/...` | `https://reyesestancias.com/...` |
| Usuario autenticado, `email_verified=False`, clic en "Enviar enlace" | Enlace a `/signup/` (inútil) | POST a `send_verification_link`, recibe email |
| Clic en el magic link estando autenticado | Escribe `guest_email` en sesión (no tiene efecto) | Marca `email_verified=True` en BD — permanente |
| Clic en el magic link siendo invitado | Sin cambios | Sin cambios (sigue usando sesión) |
| Feedback visual tras enviar el enlace | Ninguno | Banner verde con confirmación |

### Flujo corregido completo (usuario autenticado)

```
1. Usuario con cuenta va a "Mis Reservas"
2. Ve banner ámbar: "Verifica tu correo [...] Enviar enlace"
3. Clic en "Enviar enlace" → POST /bookings/mis-reservas/verificar-email/
4. Se genera MagicLink(email=user.email) y se envía email
5. Redirect a /bookings/bookings_list/?verification_sent=1
6. "Mis Reservas" muestra banner verde: "Te hemos enviado un enlace..."
7. Usuario abre el correo → clic en "Ver mis reservas"
   → URL: https://reyesestancias.com/bookings/mis-reservas/verificar/<uuid>/
8. ValidateMagicLinkView:
   - Valida token (no caducado, no usado)
   - Detecta request.user.is_authenticated + email coincide
   - user.email_verified = True → save()
   - session.cycle_key()
   - redirect bookings_list
9. "Mis Reservas" muestra banner verde permanente:
   "Mostrando todas tus reservas, incluyendo las realizadas como invitado con [email]"
10. El usuario ve sus reservas de cuenta + sus reservas de invitado unificadas
```

---

## 7. Pendiente y próximos pasos

### Pendiente obligatorio antes de producción

**1. Configurar variables de entorno en Railway**

En el dashboard de Railway, verificar/añadir:
```
SITE_BASE_URL=https://reyesestancias.com
STRIPE_SECRET_KEY=sk_live_...       # ya debería estar
STRIPE_PUBLISHABLE_KEY=pk_live_...  # ya debería estar
STRIPE_WEBHOOK_SECRET=whsec_...     # el del webhook de producción registrado en dashboard.stripe.com
```

**2. Crear superusuario en producción**

```bash
railway run python manage.py createsuperuser
```

**3. Ejecutar migraciones en producción**

```bash
railway run python manage.py migrate
```

Las migraciones nuevas son:
- `accounts/0002_user_email_verified`
- `accounts/0003_user_username_blank`
- `bookings/0014` al `bookings/0016` (+ `0015b`)

**4. Verificar webhook de producción en Stripe**

En [dashboard.stripe.com](https://dashboard.stripe.com) → Developers → Webhooks:
- El endpoint debe apuntar a `https://reyesestancias.com/payments/webhook/`
- Eventos necesarios: `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `refund.updated`, `charge.refunded`

---

### Mejoras futuras recomendadas

**Reenvío del email de verificación**

Actualmente, si el usuario no recibió el email de verificación de cuenta, no tiene forma de solicitarlo de nuevo. Se debería añadir una vista `/accounts/reenviar-verificacion/` con un formulario de email.

**Panel de administración para soporte**

Añadir en el admin de Django:
- `MagicLink` con filtros por email y estado
- `Booking` con campo `access_token` visible (para que soporte pueda enviar el link manualmente)

**Caducidad de la sesión de invitado**

Actualmente la sesión de invitado (`guest_email`) no tiene caducidad explícita — dura lo que dure la sesión de Django. Podría añadirse un timestamp para invalidarla tras X horas.

**`RemakeBookingView` para invitados**

`RemakeBookingView` sigue con `LoginRequiredMixin` — rehacer una reserva cancelada requiere tener cuenta. Para que los invitados también puedan rehacerla, habría que adaptar esta vista al flujo por token o al flujo de magic link.

**Pruebas automatizadas**

Todo el flujo descrito debería tener tests de integración que cubran:
- Reserva sin login → pago → webhook → email
- Magic link → validación → bookings_list
- Expiración de magic link
- Rate limiting

---

## 8. Referencia rápida de URLs

### Reservas (`/bookings/`)

| URL | Nombre | Descripción |
|---|---|---|
| `create_booking/<int:property_id>/` | `create_booking` | Formulario de reserva (sin login) |
| `bookings_list/` | `bookings_list` | Mis reservas (dual: cuenta o magic link) |
| `remake_booking/<int:pk>/` | `remake_booking` | Rehacer reserva cancelada (requiere login) |
| `mis-reservas/` | `request_magic_link` | Formulario email → magic link |
| `mis-reservas/verificar/<uuid>/` | `validate_magic_link` | Validar magic link |
| `mis-reservas/verificar-email/` | `send_verification_link` | Enviar magic link al usuario autenticado (POST) |
| `mis-reservas/salir/` | `clear_guest_session` | Limpiar sesión de invitado |
| `cancel/<uuid>/` | `cancel_booking_token` | Cancelar reserva (POST) |
| `cancel/<uuid>/sure/` | `cancel_booking_sure_token` | Confirmación cancelación (GET) |
| `change_dates/<uuid>/` | `booking_change_dates_start_token` | Iniciar cambio de fechas |
| `change_dates/<uuid>/preview/` | `booking_change_dates_preview_token` | Preview cambio de fechas |
| `change_dates/<uuid>/apply/` | `booking_change_dates_apply_token` | Aplicar cambio de fechas |

### Pagos (`/payments/`)

| URL | Nombre | Descripción |
|---|---|---|
| `payment_start/<uuid>/` | `payment_start_token` | Checkout depósito por token |
| `balance_start/<uuid>/` | `start_balance_token` | Checkout balance por token |
| `retry-deposit/<uuid>/` | `retry_deposit_token` | Reintentar depósito |
| `retry-balance/<uuid>/` | `retry_balance_token` | Reintentar balance |
| `payment_success/` | `payment_success` | Redirect tras pago exitoso |
| `payment_cancel/` | `payment_cancel` | Redirect tras pago cancelado |
| `webhook/` | `webhook` | Stripe webhook (CSRF exempt) |

### Cuentas (`/accounts/`)

| URL | Nombre | Descripción |
|---|---|---|
| `login/` | `login` | Login por email + contraseña |
| `logout/` | `logout` | Cerrar sesión |
| `signup/` | `signup` | Registro (sin username) |
| `verify-email/<str:token>/` | `verify_email` | Verificar email tras registro |
| `password_reset/` | `password_reset` | Recuperar contraseña |

---

## 9. Guía de pruebas locales

### Configuración para test

En `.env`, usar las claves de test de Stripe:
```
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=<generado por stripe listen>
```

### Arrancar el entorno

```bash
# Terminal 1: servidor Django
python manage.py runserver

# Terminal 2: Celery worker (para tareas asíncronas)
celery -A reyes_estancias worker -l info

# Terminal 3: Stripe CLI (para webhooks)
stripe listen --forward-to localhost:8000/payments/webhook/
# Copiar el whsec_... que imprime y pegarlo en .env como STRIPE_WEBHOOK_SECRET
# Reiniciar el servidor Django tras actualizar .env
```

### Tarjeta de test de Stripe

| Campo | Valor |
|---|---|
| Número | `4242 4242 4242 4242` |
| Fecha | Cualquier futura (ej. `12/34`) |
| CVC | `123` |
| Nombre/dirección | Cualquier valor |

Para simular pago fallido: `4000 0000 0000 0002`

### Flujo de prueba completo

1. Ve a una propiedad y selecciona fechas
2. Rellena el formulario de huésped (nombre, email, teléfono)
3. Haz clic en "Continuar al pago" → redirige a Stripe
4. Paga con la tarjeta de test
5. Verifica en Terminal 3 que aparece `[200] POST .../payments/webhook/`
6. Comprueba el email de confirmación (Mailjet o spam)
7. Ve a `/bookings/mis-reservas/`, introduce el email → comprueba el magic link en el email
8. Haz clic en el link → deberías ver la reserva en "Mis Reservas"
9. Prueba a cancelar o cambiar fechas desde los botones

### Volver a producción

En `.env`, descomentar claves `sk_live_*` y comentar `sk_test_*`. No tocar `.env.production` (Railway lo gestiona con sus propias variables de entorno).
