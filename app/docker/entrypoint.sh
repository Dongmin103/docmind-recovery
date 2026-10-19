#!/usr/bin/env bash

set -e

case "${API_PROXY_SCHEME-python}" in
    python) ;;
    *) echo "Unsupported API_PROXY_SCHEME: ${API_PROXY_SCHEME}; only python is supported" >&2; exit 1 ;;
esac
case "${DB_TYPE-mysql}" in
    mysql|MYSQL) ;;
    *) echo "Unsupported DB_TYPE: ${DB_TYPE}; only mysql is supported" >&2; exit 1 ;;
esac
case "${DOC_ENGINE-elasticsearch}" in
    elasticsearch) ;;
    *) echo "Unsupported DOC_ENGINE: ${DOC_ENGINE}; only elasticsearch is supported" >&2; exit 1 ;;
esac
case "${STORAGE_IMPL-MINIO}" in
    MINIO|minio) ;;
    *) echo "Unsupported STORAGE_IMPL: ${STORAGE_IMPL}; only MINIO is supported" >&2; exit 1 ;;
esac

echo "Start DOCMIND, version: "
cat /ragflow/VERSION

# -----------------------------------------------------------------------------
# Usage and command-line argument parsing
# -----------------------------------------------------------------------------
function usage() {
    echo "Usage: $0 [OPTIONS]"
    echo
    echo "  --disable-webserver                     Disables the web server (nginx + ragflow_server)."
    echo "  --init-model-provider-tables            Run model provider table migrations and exit."
    echo "  --init-superuser                        Initializes the superuser."
    echo
    echo "Examples:"
    echo "  $0 --init-superuser"
    exit 1
}

# DocMind cloud ingestion runs TaskHandler inside the API. There are no
# standalone queue consumers or external connector sync daemons to launch.
ENABLE_WEBSERVER=1 # Default to enable web server
INIT_SUPERUSER_ARGS="" # Default to not initialize superuser
INIT_MODEL_PROVIDER_TABLES=0


# Parse arguments
for arg in "$@"; do
  case $arg in
    --disable-webserver)
      ENABLE_WEBSERVER=0
      shift
      ;;
    --init-model-provider-tables)
      INIT_MODEL_PROVIDER_TABLES=1
      shift
      ;;
    --init-superuser)
      INIT_SUPERUSER_ARGS="--init-superuser"
      shift
      ;;
    *)
      usage
      ;;
  esac
done

# -----------------------------------------------------------------------------
# Replace env variables in the service_conf.yaml file
# -----------------------------------------------------------------------------
CONF_DIR="/ragflow/conf"
TEMPLATE_FILE="${CONF_DIR}/service_conf.yaml.template"
CONF_FILE="${CONF_DIR}/service_conf.yaml"

rm -f "${CONF_FILE}"
DEF_ENV_VALUE_PATTERN="\$\{([^:]+):-([^}]+)\}"
while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ DEF_ENV_VALUE_PATTERN ]]; then
        varname="${BASH_REMATCH[1]}"
        default="${BASH_REMATCH[2]}"

        if [ -n "${!varname}" ]; then
            eval "echo \"$line"\" >> "${CONF_FILE}"
        else
            echo "$line" | sed -E "s/\\\$\{[^:]+:-([^}]+)\}/\1/g" >> "${CONF_FILE}"
        fi
    else
        eval "echo \"$line\"" >> "${CONF_FILE}"
    fi
done < "${TEMPLATE_FILE}"

export LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu/"
PY=python3

# -----------------------------------------------------------------------------
# Select the Python Nginx configuration
# -----------------------------------------------------------------------------
NGINX_CONF_DIR="/etc/nginx/conf.d"
cp -f "$NGINX_CONF_DIR/ragflow.conf.python" "$NGINX_CONF_DIR/ragflow.conf"

# -----------------------------------------------------------------------------
# Function(s)
# -----------------------------------------------------------------------------


function ensure_db_init() {
    echo "Initializing database tables..."
    "$PY" -c "from api.db.db_models import init_database_tables as init_web_db; init_web_db()"
    echo "Database tables initialized."
}

# -----------------------------------------------------------------------------
# Start components based on flags
# -----------------------------------------------------------------------------

run_with_restart() {
  local process_name="$1"
  shift

  while true; do
    echo "Attempt to start ${process_name}..."
    set +e
    "$@"
    local exit_code=$?
    set -e
    echo "${process_name} exited with code ${exit_code}. Restarting in 1 second..."
    sleep 1
  done
}

if [[ "${INIT_MODEL_PROVIDER_TABLES}" -eq 1 ]]; then
    tools/scripts/run_migrations.sh
fi

if [[ "${ENABLE_WEBSERVER}" -eq 1 ]]; then
    ensure_db_init

    echo "Starting nginx..."
    /usr/sbin/nginx -c /etc/nginx/nginx.conf

    echo "Attempt to start RAGFlow python server..."
    run_with_restart "RAGFlow python server" "$PY" api/ragflow_server.py ${INIT_SUPERUSER_ARGS} &

fi


wait
