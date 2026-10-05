#!/bin/bash
set -euo pipefail

echo "=== Switch Day-0 Installation Script ==="
echo ""

INSTALL_DIR="/opt/switch-day0"
SERVICE_USER="switchday0"
SERVICE_NAME="switch-day0.service"
REQUIRED_PY_MAJOR=3
REQUIRED_PY_MINOR_MIN=9
LISTEN_PORT=8001

if [ "${EUID}" -ne 0 ]; then
    echo "This script must be run with sudo."
    echo "Example: sudo bash scripts/install.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ ! -f "${PROJECT_ROOT}/app/main.py" ] || [ ! -f "${PROJECT_ROOT}/requirements.txt" ]; then
    echo "Could not locate the Switch Day-0 project root from ${SCRIPT_DIR}."
    echo "Run: sudo bash scripts/install.sh"
    exit 1
fi

if [ ! -f "${PROJECT_ROOT}/systemd/switch-day0.service" ]; then
    echo "Missing ${PROJECT_ROOT}/systemd/switch-day0.service"
    exit 1
fi

if [ ! -f "${PROJECT_ROOT}/.env.example" ]; then
    echo "Missing ${PROJECT_ROOT}/.env.example"
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    ca-certificates \
    iproute2 \
    openssl \
    python3 \
    python3-pip \
    python3-venv \
    rsync

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "Python 3 is not installed."
    exit 1
fi

PY_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VERSION%%.*}"
PY_MINOR="${PY_VERSION#*.}"

if [ "${PY_MAJOR}" -lt "${REQUIRED_PY_MAJOR}" ] || { [ "${PY_MAJOR}" -eq "${REQUIRED_PY_MAJOR}" ] && [ "${PY_MINOR}" -lt "${REQUIRED_PY_MINOR_MIN}" ]; }; then
    echo "Switch Day-0 requires Python ${REQUIRED_PY_MAJOR}.${REQUIRED_PY_MINOR_MIN} or newer."
    echo "Found: Python ${PY_VERSION} (${PYTHON_BIN})"
    exit 1
fi

echo "Using ${PYTHON_BIN} (Python ${PY_VERSION})"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Creating system user: ${SERVICE_USER}"
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
else
    echo "System user ${SERVICE_USER} already exists"
fi

echo "Creating installation directory..."
mkdir -p "${INSTALL_DIR}"

systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

if ss -ltnH | awk '{print $4}' | grep -Eq ":${LISTEN_PORT}$"; then
    echo "Port ${LISTEN_PORT} is already in use."
    echo "Switch Day-0 needs port ${LISTEN_PORT}."
    ss -ltnp | grep -E ":${LISTEN_PORT}\\b" || true
    exit 1
fi

echo "Copying application files from ${PROJECT_ROOT}..."
rsync -a \
    --exclude '.git/' \
    --exclude 'venv/' \
    --exclude '.venv/' \
    --exclude 'data/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '*.pyc' \
    --exclude '.env' \
    "${PROJECT_ROOT}/" "${INSTALL_DIR}/"

mkdir -p "${INSTALL_DIR}/data"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "Setting up Python virtual environment..."
rm -rf "${INSTALL_DIR}/venv"
sudo -u "${SERVICE_USER}" "${PYTHON_BIN}" -m venv "${INSTALL_DIR}/venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/pip" install --no-cache-dir --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/pip" install --no-cache-dir -r "${INSTALL_DIR}/requirements.txt"

CREATED_ENV=0
ADMIN_USER="admin"
ADMIN_PASSWORD=""

echo "Setting up configuration..."
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    ADMIN_PASSWORD="$(openssl rand -hex 16)"
    SECRET="$(openssl rand -hex 32)"
    sed \
        -e "s/^SWITCH_DAY0_ADMIN_PASSWORD=.*/SWITCH_DAY0_ADMIN_PASSWORD=${ADMIN_PASSWORD}/" \
        -e "s/^SWITCH_DAY0_SECRET=.*/SWITCH_DAY0_SECRET=${SECRET}/" \
        "${INSTALL_DIR}/.env.example" > "${INSTALL_DIR}/.env"
    chmod 600 "${INSTALL_DIR}/.env"
    ADMIN_USER="$(grep -E '^SWITCH_DAY0_ADMIN_USERNAME=' "${INSTALL_DIR}/.env" | cut -d= -f2-)"
    CREATED_ENV=1
    echo "  → Created .env with a generated admin password and session secret"
else
    echo "  → Left existing .env in place"
fi

chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.env" "${INSTALL_DIR}/data"

echo "Installing systemd service..."
cp "${PROJECT_ROOT}/systemd/switch-day0.service" /etc/systemd/system/switch-day0.service

systemctl daemon-reload
if ! systemctl enable --now "${SERVICE_NAME}"; then
    echo ""
    echo "Failed to start ${SERVICE_NAME}."
    systemctl status "${SERVICE_NAME}" --no-pager || true
    journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
    exit 1
fi

if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
    echo ""
    echo "The service did not stay running."
    systemctl status "${SERVICE_NAME}" --no-pager || true
    journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
    exit 1
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "${HOST_IP}" ]; then
    HOST_IP="127.0.0.1"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
if [ "${CREATED_ENV}" -eq 1 ]; then
    echo "Admin username: ${ADMIN_USER}"
    echo "Admin password: ${ADMIN_PASSWORD}"
    echo "Saved in ${INSTALL_DIR}/.env"
    echo "The password is used only to create the first account."
    echo ""
else
    echo "Sign in with the account already stored in ${INSTALL_DIR}/data/."
    echo "The existing ${INSTALL_DIR}/.env file was not changed."
    echo ""
fi
echo "Open the web interface:"
echo "   http://${HOST_IP}:${LISTEN_PORT}/"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status switch-day0"
echo "  sudo systemctl restart switch-day0"
echo "  journalctl -u switch-day0 -f"
