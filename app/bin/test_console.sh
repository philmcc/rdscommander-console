#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "${SCRATCH}"' EXIT
mkdir -p "${SCRATCH}/repo/config"
cp "${HERE}/../config.example/environments.yaml" "${SCRATCH}/repo/config/"
export RDSC_REPO_ROOT="${SCRATCH}/repo" RDSC_ENV_FILE="${SCRATCH}/repo/.env"
export GIT_CONFIG_GLOBAL="${SCRATCH}/gitconfig"
git config --global user.name "Test Operator"
git config --global user.email "test@example.invalid"

# 1. profiles with no env file content: bundled db + simulated aws
: > "${RDSC_ENV_FILE}"
out="$("${HERE}/console" profiles | tr '\n' ' ')"
[[ "${out}" == *"bundled-db"* && "${out}" == *"simulated-aws"* ]] || { echo "expected both profiles, got: ${out}"; exit 1; }

# 2. shared database url disables the bundled db profile
echo "RDSC_CONTROL_DATABASE_URL=postgresql+psycopg://u:p@h/d" > "${RDSC_ENV_FILE}"
out="$("${HERE}/console" profiles | tr '\n' ' ')"
[[ "${out}" != *"bundled-db"* && "${out}" == *"simulated-aws"* ]] || { echo "expected only simulated-aws, got: ${out}"; exit 1; }

# 3. env file is written from git identity when missing
rm -f "${RDSC_ENV_FILE}"
sed -n '/^write_env_file()/,/^}/p' "${HERE}/console" > "${SCRATCH}/write_env_file.sh"
(
  # shellcheck disable=SC1090
  source "${SCRATCH}/write_env_file.sh"
  log() { :; }
  ENV_FILE="${RDSC_ENV_FILE}"
  write_env_file
)
grep -q '^RDSC_OPERATOR_NAME=Test Operator' "${RDSC_ENV_FILE}" || { echo "env file missing operator name"; cat "${RDSC_ENV_FILE}"; exit 1; }

# 4. compose file is valid with every profile
(cd "${HERE}/.." && RDSC_ENV_FILE="${RDSC_ENV_FILE}" docker compose -f compose.yaml --profile bundled-db --profile simulated-aws config -q)

# 5. values in the operator env file reach the compose file: shared database URL and custom port
cat > "${RDSC_ENV_FILE}" <<EOF_ENV
RDSC_OPERATOR_NAME=Test Operator
RDSC_CONTROL_DATABASE_URL=postgresql+psycopg://u:p@shared-host:5432/rdscommander
RDSC_HTTP_PORT=9999
EOF_ENV
rendered="$(cd "${HERE}/.." && RDSC_ENV_FILE="${RDSC_ENV_FILE}" RDSC_REPO_ROOT="${RDSC_REPO_ROOT}" docker compose -f compose.yaml --env-file "${RDSC_ENV_FILE}" --profile simulated-aws config)"
echo "${rendered}" | grep -q 'RDSC_CONTROL_DATABASE_URL: postgresql+psycopg://u:p@shared-host:5432/rdscommander' || { echo "shared database URL did not reach the backend"; echo "${rendered}" | grep -n RDSC_CONTROL_DATABASE_URL; exit 1; }
echo "${rendered}" | grep -q 'published: "9999"' || { echo "custom port did not reach the published port"; echo "${rendered}" | grep -n -A2 'ports:'; exit 1; }
echo "${rendered}" | grep -q '/root/.ssh' || { echo "ssh directory is not mounted"; exit 1; }
echo "${rendered}" | grep -q '/root/.gitconfig' || { echo "git configuration is not mounted"; exit 1; }
echo "${rendered}" | grep -q 'hostname: rdscommander-console' || { echo "the backend has no fixed hostname"; exit 1; }
echo "console script tests passed"
