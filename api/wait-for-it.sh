#!/bin/bash

set -e

host=$1
shift
cmd="$@"

until mysql -h db -u root -p"$MYSQL_ROOT_PASSWORD"; do
  >&2 echo "mysql is unavailable - sleeping"
  sleep 1
done

>&2 echo "mysql is up - executing command"
exec bash -c 'python manage.py db upgrade || gunicorn --bind 0.0.0.0:8000 -w 4 app:app'