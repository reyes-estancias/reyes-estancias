web: python manage.py collectstatic --no-input --skip-checks && python manage.py migrate --no-input && gunicorn reyes_estancias.wsgi:application --bind 0.0.0.0:$PORT
worker: celery -A reyes_estancias worker --loglevel=info --concurrency=2
beat: celery -A reyes_estancias beat --loglevel=info
