# Notificaciones de nueva reserva

Cada vez que un cliente completa una reserva, el sistema lanza automáticamente dos tipos de notificación a los propietarios.

---

## Vías de notificación activas

### 1. Correo electrónico
Se manda un email con el resumen completo de la reserva a:
- `reyesestancias@gmail.com`
- `jos-reyes10@hotmail.com`

Configurado en `settings.py`:
```python
OWNER_NOTIFICATION_EMAILS = ['reyesestancias@gmail.com', 'jos-reyes10@hotmail.com']
```

En local usa Mailtrap. En producción usa Mailjet (requiere `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` en las variables de entorno de Railway).

### 2. Telegram
Se manda un mensaje al bot de Telegram con el mismo contenido que el email: datos del cliente, fechas, horarios de check-in/out, número de noches, huéspedes e importes (total, anticipo cobrado y saldo pendiente).

El código de ambas notificaciones está en `payments/services.py`, función `send_booking_confirmation_email`.

---

## Configuración del bot de Telegram

### Bot creado
- **Nombre:** Reyes Estancias Notificaciones
- **Username:** @reyesestancias_bot
- **Link directo:** t.me/reyesestancias_bot

### Variables de entorno necesarias
```
TELEGRAM_BOT_TOKEN=<token del bot>
TELEGRAM_CHAT_IDS=1386855196,OTRO_CHAT_ID,...
```

`TELEGRAM_CHAT_IDS` acepta múltiples IDs separados por coma — el bot manda el mensaje a todos ellos.

---

## Cómo añadir a una nueva persona

1. La persona abre este link en su móvil: **t.me/reyesestancias_bot**
2. Le da a **Iniciar** y le escribe cualquier mensaje (un "hola")
3. Accede a esta URL en el navegador para ver su `chat_id`:
   ```
   https://api.telegram.org/bot8361140230:AAE1ebzB4cpODvzmgBg-UN_6W5EdgTP-1Sg/getUpdates
   ```
4. En el JSON que devuelve, el `chat_id` es el número que aparece en `"from": {"id": XXXXXXX}`
5. Añade ese número a `TELEGRAM_CHAT_IDS` separado por coma (en local en `.env`, en producción en Railway)

---

## Dónde está implementado

| Archivo | Qué hace |
|---|---|
| `payments/services.py` | Función `_send_telegram_booking_notification` — construye y envía el mensaje |
| `reyes_estancias/settings.py` | Lee `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_IDS` del entorno |
| `.env` | Variables locales (no se sube a git) |
| Railway → Variables | Variables en producción |
