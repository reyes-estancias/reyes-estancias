"""
Script de uso único para subir las imágenes existentes de media/ a Cloudinary.
Ejecutar desde la raíz del proyecto con el venv activado:
    python scripts/upload_media_to_cloudinary.py
"""
import os
import sys
import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'reyes_estancias.settings')
django.setup()

import cloudinary.uploader

MEDIA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'media')

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.JPG', '.JPEG', '.PNG'}

ok = 0
errors = 0

for root, dirs, files in os.walk(MEDIA_ROOT):
    for filename in files:
        ext = os.path.splitext(filename)[1]
        if ext not in IMAGE_EXTENSIONS:
            continue

        filepath = os.path.join(root, filename)
        relative_path = os.path.relpath(filepath, MEDIA_ROOT).replace('\\', '/')
        public_id = os.path.splitext(relative_path)[0]

        try:
            cloudinary.uploader.upload(
                filepath,
                public_id=public_id,
                overwrite=True,
                resource_type='image',
            )
            print(f"✓ {relative_path}")
            ok += 1
        except Exception as e:
            print(f"✗ {relative_path} → {e}")
            errors += 1

print(f"\nSubidas: {ok} | Errores: {errors}")
