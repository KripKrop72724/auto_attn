#!/bin/sh
set -eu

test -f .env.add || { echo "Missing .env.add" >&2; exit 1; }
docker compose --env-file .env.add -f docker-compose.add.yml config --quiet
docker compose --env-file .env.add -f docker-compose.add.yml build --pull
docker compose --env-file .env.add -f docker-compose.add.yml up -d --remove-orphans
docker compose --env-file .env.add -f docker-compose.add.yml ps
