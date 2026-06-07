#!/bin/sh
set -e

mkdir -p /app/data
chown -R app:app /app/data

exec gosu app "$@"

