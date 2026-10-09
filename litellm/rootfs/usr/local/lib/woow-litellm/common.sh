# shellcheck shell=sh
# Shared helpers for the s6-rc scripts of the Woow LiteLLM add-on. Sourced, never executed.
# shellcheck disable=SC2034  # used by the scripts that source this file
WOOW_LIB=/usr/local/lib/woow-litellm
PGSOCK=/run/postgresql

log() { printf '[woow-litellm] %s: %s\n' "${WOOW_STEP:-woow}" "$*" >&2; }
fatal() { log "FATAL $*"; exit 1; }
woow_init() { "$WOOW_LIB/woow-init" "$@"; }
as_postgres() { /command/s6-setuidgid postgres "$@"; }
# psql as the postgres superuser over the unix socket (peer authentication); values never on argv.
psql_su() { as_postgres /usr/bin/psql -X -q -tA -v ON_ERROR_STOP=1 -h "$PGSOCK" -p 5432 -U postgres "$@"; }
