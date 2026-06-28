web: python manage.py collectstatic --no-input && python manage.py migrate --no-input && gunicorn reyes_estancias.wsgi:application --bind 0.0.0.0:$PORT
worker: celery -A reyes_estancias worker --loglevel=info
beat: celery -A reyes_estancias beat --loglevel=info
