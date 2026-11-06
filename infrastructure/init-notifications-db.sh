#!/bin/sh
set -eu

# Only for a new v1.3 volume; existing owner roles and databases are untouched.
psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=notifications_password="$NOTIFICATIONS_DB_PASSWORD" <<'SQL'
CREATE ROLE fulfillflow_notifications LOGIN PASSWORD :'notifications_password';
CREATE DATABASE fulfillflow_notifications OWNER fulfillflow_notifications;
REVOKE ALL ON DATABASE fulfillflow_notifications FROM PUBLIC;
SQL
