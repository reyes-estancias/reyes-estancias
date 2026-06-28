# Deploy en Railway + Cloudinary — Guía completa

## Qué se hizo y por qué

### 1. Driver de MySQL: mysqlclient → PyMySQL

`mysqlclient` requiere compilar librerías C nativas (`libmysqlclient-dev`) que no están disponibles en el entorno Nixpacks de Railway. Se sustituyó por `PyMySQL`, un driver puro Python que no necesita compilación.

**Archivos cambiados:**
- `requirements.txt`: `mysqlclient` → `PyMySQL==1.1.1` + `cryptography` (necesario para MySQL 8.0 con `caching_sha2_password`)
- `nixpacks.toml`: se vaciaron los paquetes apt (ya no hacen falta)
- `reyes_estancias/__init__.py`: se añadió `pymysql.install_as_MySQLdb()` antes de importar Celery

### 2. collectstatic en el Procfile

`collectstatic` se movió del `buildCommand` de `railway.json` al inicio del proceso web en `Procfile`. Motivo: durante el build no están disponibles las variables de entorno de Railway (SECRET_KEY, etc.), y Django las necesita para arrancar.

**Procfile actual:**
```
web: python manage.py collectstatic --no-input --skip-checks && python manage.py migrate --no-input && gunicorn reyes_estancias.wsgi:application --bind 0.0.0.0:$PORT
worker: celery -A reyes_estancias worker --loglevel=info --concurrency=2
beat: celery -A reyes_estancias beat --loglevel=info
```

> `--skip-checks` evita que collectstatic intente conectarse a la base de datos durante la recolección.

### 3. Variables de entorno compartidas en Railway

Todas las variables de entorno se configuraron como **Shared Variables** en Railway para que los 5 servicios (web, worker, beat, MySQL, Redis) las reciban sin duplicar configuración.

Variables necesarias: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DB_*`, `CLOUDINARY_*`, `STRIPE_*`, `SENDGRID_*`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `SITE_BASE_URL`.

### 4. Importar la base de datos local a Railway

Se usó `mysqldump` directamente contra el endpoint público de Railway (el interno `mysql.railway.internal` solo es accesible desde dentro de Railway).

```bash
# Exportar desde MySQL local (ignorando tablas de sistema que Railway ya crea con migrate)
mysqldump -u reyes_web -pjose-reyes reyes_estancias \
  --no-tablespaces --insert-ignore \
  --ignore-table=reyes_estancias.django_migrations \
  > /tmp/datos.sql

# Importar al MySQL de Railway (credenciales en Railway → MySQL → Connect → Public Network)
mysql -h reseau.proxy.rlwy.net -u root -p<PASSWORD> --port 40433 --protocol=TCP railway < /tmp/datos.sql
```

### 5. Cloudinary para imágenes de propiedades

Las imágenes de propiedades son archivos **media** (subidos por el usuario), no estáticos. En Railway el sistema de archivos es efímero: cualquier archivo subido en producción desaparece al reiniciar el contenedor. Cloudinary soluciona esto almacenando las imágenes en la nube de forma persistente.

**Configuración en `settings.py`:**
```python
INSTALLED_APPS = [
    ...
    'django.contrib.staticfiles',
    'cloudinary_storage',  # debe ir DESPUÉS de staticfiles
    'cloudinary',
    ...
]

STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

CLOUDINARY_STORAGE = {
    'CLOUD_NAME': env('CLOUDINARY_CLOUD_NAME'),
    'API_KEY': env('CLOUDINARY_API_KEY'),
    'API_SECRET': env('CLOUDINARY_API_SECRET'),
    'PREFIX': '',  # sin prefijo: public_id = 'properties/imagen.jpg'
}
```

> En Django 5.2 `DEFAULT_FILE_STORAGE` y `STATICFILES_STORAGE` fueron eliminados. Hay que usar `STORAGES`.

Las imágenes ya existentes en local se subieron a Cloudinary con el script `scripts/upload_media_to_cloudinary.py`.

### 6. Whitenoise: CompressedStaticFilesStorage

`CompressedManifestStaticFilesStorage` lanzaba un 500 en el admin porque Jazzmin referencia `vendor/bootswatch` en sus templates pero ese archivo no quedaba en el manifest. Se cambió a `CompressedStaticFilesStorage`, que comprime los archivos igualmente pero sin validación estricta del manifest.

---

## Arquitectura actual en producción

```
Railway
├── web       → gunicorn (Django 5.2)
├── worker    → Celery worker (concurrency=2)
├── beat      → Celery beat (tareas programadas)
├── MySQL     → Base de datos principal
└── Redis     → Broker de Celery

Cloudinary   → Almacenamiento persistente de imágenes de propiedades
Stripe       → Pagos
SendGrid     → Emails transaccionales
```

---

## Cómo añadir contenido en el futuro

### Añadir una nueva propiedad con sus fotos

**Opción A — Desde el admin de Django (recomendada):**

1. Entra en `https://web-production-d8c4f.up.railway.app/admin/`
2. Ve a **Properties → Properties → Añadir propiedad**
3. Rellena todos los campos y guarda
4. Ve a **Properties → Images → Añadir imagen**, selecciona la propiedad y sube la foto
5. Cloudinary recibe la imagen automáticamente al guardar — no hay que hacer nada más

**Opción B — Desde local con importación a Railway:**

1. Crea la propiedad en local (admin o shell)
2. Exporta solo esas filas:
```bash
mysqldump -u reyes_web -pjose-reyes reyes_estancias properties_property properties_propertyimage \
  --no-tablespaces --insert-ignore > /tmp/nuevas_propiedades.sql
mysql -h reseau.proxy.rlwy.net -u root -p<PASSWORD> --port 40433 --protocol=TCP railway < /tmp/nuevas_propiedades.sql
```
3. Sube las imágenes a Cloudinary con el script:
```bash
python scripts/upload_media_to_cloudinary.py
```

### Subir imágenes sueltas a Cloudinary

Si en algún momento necesitas subir imágenes manualmente (por ejemplo, tras una migración):

```bash
# Desde la raíz del proyecto con el venv activado
python scripts/upload_media_to_cloudinary.py
```

El script recorre toda la carpeta `media/`, sube cada imagen con `overwrite=True` y usa como public_id la ruta relativa sin extensión (ej: `properties/mi-foto`). Es seguro ejecutarlo varias veces.

### Modificar datos directamente en la base de datos de Railway

```bash
# Conexión directa (credenciales en Railway → MySQL → Connect → Public Network)
mysql -h reseau.proxy.rlwy.net -u root -p<PASSWORD> --port 40433 --protocol=TCP railway
```

### Hacer una copia de seguridad de la base de datos de Railway

```bash
mysqldump -h reseau.proxy.rlwy.net -u root -p<PASSWORD> --port 40433 --protocol=TCP \
  --no-tablespaces railway > backup_$(date +%Y%m%d).sql
```

---

## Credenciales y accesos

| Servicio | Dónde están |
|----------|-------------|
| Railway (MySQL, Redis, vars) | Dashboard de Railway → proyecto Reyes Estancias |
| Cloudinary | cloudinary.com → cuenta ekamartinc2003@gmail.com |
| Stripe | dashboard.stripe.com |
| SendGrid | app.sendgrid.com |

> Las credenciales de Cloudinary también están en `.env` local (no subido a git).

---

## Comandos útiles para Railway CLI

```bash
# Conectar el proyecto local con Railway
railway link

# Ver logs en tiempo real del servicio web
railway logs --service web

# Abrir una shell en el contenedor web
railway run bash

# Ejecutar un comando Django en Railway
railway run python manage.py shell
railway run python manage.py createsuperuser
```
