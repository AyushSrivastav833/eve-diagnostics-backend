#!/bin/sh
# Wait for the database, apply migrations, then run the given command.
set -e

if [ "${SKIP_MIGRATIONS:-false}" != "true" ]; then
  python - <<'PY'
import os, sys, time
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.db import connection
for attempt in range(30):
    try:
        connection.ensure_connection()
        break
    except Exception as exc:
        print(f"waiting for database ({exc.__class__.__name__})...", flush=True)
        time.sleep(1)
else:
    sys.exit("database not reachable")
PY
  python manage.py migrate --noinput
fi

exec "$@"
