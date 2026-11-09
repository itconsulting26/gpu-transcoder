#!/bin/bash
set -e
cd /app
[ "$1" = "gunicorn" ] || sleep 6
python3 manage.py migrate --noinput
if [ "$1" = "gunicorn" ]; then
  python3 manage.py collectstatic --noinput >/dev/null
  python3 manage.py ensure_admin
fi
exec "$@"
