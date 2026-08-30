#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/home/valk/valk}"
APP_USER="${APP_USER:-valk}"
APP_GROUP="${APP_GROUP:-$APP_USER}"
SERVICE_NAME="${SERVICE_NAME:-valkflask.service}"
CONFIGURE_NGINX="${CONFIGURE_NGINX:-1}"
STAMP="$(date +%Y%m%d-%H%M%S)"

log() {
    printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        die "Run this script as root, for example: sudo APP_DIR=$APP_DIR bash $0"
    fi
}

backup_file() {
    local path="$1"
    if [[ -e "$path" || -L "$path" ]]; then
        cp -a "$path" "$path.backup.$STAMP"
        log "Backed up $path to $path.backup.$STAMP"
    fi
}

write_service() {
    local service_path="/etc/systemd/system/$SERVICE_NAME"
    backup_file "$service_path"
    cat > "$service_path" <<SERVICE
[Unit]
Description=VALK Flask Server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/app.py
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE
    chmod 0644 "$service_path"
}

write_nginx() {
    local available="/etc/nginx/sites-available/valkflask"
    local enabled="/etc/nginx/sites-enabled/valkflask"
    backup_file "$available"
    cat > "$available" <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    server_name _;
    client_max_body_size 20m;

    access_log /var/log/nginx/valkflask.access.log;
    error_log /var/log/nginx/valkflask.error.log;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
        proxy_send_timeout 300;
    }
}
NGINX
    ln -sfn "$available" "$enabled"
    if [[ -L /etc/nginx/sites-enabled/default || -e /etc/nginx/sites-enabled/default ]]; then
        backup_file /etc/nginx/sites-enabled/default
        rm -f /etc/nginx/sites-enabled/default
    fi
    nginx -t
    systemctl enable nginx
    systemctl reload-or-restart nginx
}

rebuild_venv() {
    cd "$APP_DIR"
    local new_venv=".venv.debian13.$STAMP"
    local requirements_tmp

    [[ ! -e "$new_venv" && ! -L "$new_venv" ]] || die "Temporary venv already exists: $APP_DIR/$new_venv"

    python3 -m venv "$new_venv"
    "$new_venv/bin/python" -m pip install --upgrade pip wheel setuptools

    requirements_tmp="$(mktemp)"
    grep -Ev '^[[:space:]]*pip([[:space:]<>=~!].*)?$' requirements.txt > "$requirements_tmp"
    "$new_venv/bin/python" -m pip install -r "$requirements_tmp"
    rm -f "$requirements_tmp"
    "$new_venv/bin/python" -m playwright install chromium

    "$new_venv/bin/python" -c "import matplotlib, openai, playwright; import app; print('dependency and app imports ok')"

    if [[ -e .venv || -L .venv ]]; then
        mv .venv ".venv.backup.$STAMP"
        log "Moved old .venv to $APP_DIR/.venv.backup.$STAMP"
    fi

    if [[ -e venv || -L venv ]]; then
        mv venv "venv.backup.$STAMP"
        log "Moved old venv to $APP_DIR/venv.backup.$STAMP"
    fi

    mv "$new_venv" .venv
    chown -R "$APP_USER:$APP_GROUP" .venv
}

main() {
    require_root

    [[ -d "$APP_DIR" ]] || die "App directory not found: $APP_DIR"
    [[ -f "$APP_DIR/app.py" ]] || die "app.py not found in $APP_DIR"
    [[ -f "$APP_DIR/requirements.txt" ]] || die "requirements.txt not found in $APP_DIR"
    id "$APP_USER" >/dev/null 2>&1 || die "User not found: $APP_USER"

    log "Installing Debian runtime packages"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        build-essential \
        ca-certificates \
        curl \
        nginx \
        python3 \
        python3-dev \
        python3-venv

    log "Ensuring writable runtime directories"
    install -d -o "$APP_USER" -g "$APP_GROUP" "$APP_DIR/logs" "$APP_DIR/db" "$APP_DIR/cache" "$APP_DIR/instance"

    log "Rebuilding Python virtual environment for the upgraded OS"
    rebuild_venv

    log "Writing systemd unit"
    write_service
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    sleep 2

    log "Checking service status"
    systemctl --no-pager --full status "$SERVICE_NAME" || true

    if [[ "$CONFIGURE_NGINX" == "1" ]]; then
        log "Configuring nginx reverse proxy on port 80"
        write_nginx
    else
        log "Skipping nginx configuration because CONFIGURE_NGINX=$CONFIGURE_NGINX"
    fi

    log "Local health checks"
    curl -fsS http://127.0.0.1:5000/ || true
    curl -fsS http://127.0.0.1:5555/ || true
    if [[ "$CONFIGURE_NGINX" == "1" ]]; then
        curl -fsS http://127.0.0.1/ || true
    fi

    log "Done. Recent service logs:"
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager
}

main "$@"
