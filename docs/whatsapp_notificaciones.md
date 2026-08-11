# WhatsApp: Notificaciones de reserva a Jose Senior

## Contexto del negocio

- **Ekaitz**: Programador de la app
- **Jose Senior**: Dueño del negocio. Es quien tiene que recibir el aviso cuando un cliente reserva.
- **Cliente**: Realiza la reserva a través de la web.

## Requisito

Cada vez que un cliente completa una reserva, la aplicación Django debe enviar automáticamente un WhatsApp a Jose Senior avisándole.

Actualmente esto se hace por correo electrónico. El código relevante está en:
- `payments/services.py` línea ~532 — lógica de notificación a propietarios (`OWNER_NOTIFICATION_EMAILS`)
- La plantilla del email está en `templates/emails/booking_notification_owner.html`

El aviso por WhatsApp iría en ese mismo punto del código, junto al `send_mail` existente o en sustitución de él.

---

## Opción 1: Twilio (intermediario)

Twilio es un servicio que actúa de intermediario entre tu app Django y WhatsApp.

### Cómo funciona
```
App Django → Twilio API → WhatsApp → Jose Senior
```

### Configuración
- Crear cuenta en twilio.com
- Instalar SDK: `pip install twilio`
- Añadir `TWILIO_ACCOUNT_SID` y `TWILIO_AUTH_TOKEN` a las variables de entorno
- Tiene **sandbox gratuito** para probar sin trámites

### Código de ejemplo
```python
from twilio.rest import Client

client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
client.messages.create(
    from_='whatsapp:+14155238886',  # número Twilio WhatsApp
    to='whatsapp:+34XXXXXXXXX',     # número de Jose Senior
    body=f"Nueva reserva en {booking.property.name} - {booking.guest_name}"
)
```

### Precio
- ~$0.03–$0.05 USD por mensaje enviado a Jose Senior
- Con 40 reservas/mes → ~$1.50 USD/mes
- No hay coste fijo mensual

### Ventajas
- Configuración en ~30 minutos
- Sin trámites con Meta
- SDK bien documentado para Python/Django

### Desventajas
- Más caro por mensaje que Meta directa (paga comisión de intermediario)

---

## Opción 2: Meta Cloud API (directa, sin intermediario)

Conectas tu app directamente a la API oficial de WhatsApp de Meta, sin pasar por Twilio.

### Cómo funciona
```
App Django → Meta Cloud API → WhatsApp → Jose Senior
```

### Configuración (más compleja)
1. Crear app en Meta Developer Portal
2. Verificar el negocio en Facebook Business Manager
3. Registrar un número de teléfono para WhatsApp Business
4. Crear plantillas de mensaje y esperar aprobación de Meta (puede tardar horas o días)

### Precio
El mensaje automático que manda la app a Jose Senior es una "utility conversation" (iniciada por el negocio):
- Número **mexicano** de Jose Senior: ~$0.014 USD por mensaje
- Número **español** de Jose Senior: ~$0.08 USD por mensaje

Con 40 reservas/mes:
- MX: ~$0.56 USD/mes
- ES: ~$3.20 USD/mes

Además, las primeras **1.000 conversaciones de servicio al mes son gratis**.

### Ventajas
- Más barato que Twilio a largo plazo
- Sin intermediario

### Desventajas
- Configuración de 2-3 días (aprobaciones de Meta)
- Más burocracia inicial

---

## Pregunta clave: ¿Jose Senior puede hablar con el cliente desde su número personal?

**Sí.** El flujo sería:

1. La app manda el aviso automático al **número WhatsApp Business** de Jose Senior
2. Jose Senior ve la notificación y si quiere contactar al cliente, lo hace desde su **WhatsApp personal** como una conversación normal

Esto no tiene ningún coste adicional porque ya no pasa por la API — es simplemente Jose Senior usando su móvil de forma normal.

---

## Comparativa rápida

| | Twilio | Meta directa |
|---|---|---|
| Tiempo de configuración | ~30 minutos | 2-3 días |
| Precio con ~40 reservas/mes | ~$1.50 USD | ~$0.56–$3.20 USD |
| Intermediario | Sí | No |
| Sandbox gratuito para probar | Sí | No |

---

## Decisión pendiente

Hay que decidir entre Opción 1 (Twilio, más rápido) y Opción 2 (Meta directa, más barato).

Para el volumen actual del negocio la diferencia de precio es menos de 2€/mes, por lo que el factor decisivo es la urgencia: si se quiere implementar rápido → **Twilio**.
