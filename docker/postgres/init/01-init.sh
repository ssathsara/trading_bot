#!/bin/sh
# Executed once when the postgres volume is first created.
# Store and compare all timestamps in UTC.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "ALTER DATABASE \"$POSTGRES_DB\" SET timezone TO 'UTC';"
