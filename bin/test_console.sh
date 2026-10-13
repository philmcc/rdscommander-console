#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "${SCRATCH}"' EXIT
export RDSC_HOME_DIR="${SCRATCH}/home" RDSC_ENV_FILE="${SCRATCH}/.env"
export GIT_CONFIG_GLOBAL="${SCRATCH}/gitconfig"
git config --global user.name "Test Operator"
git config --global user.email "test@example.invalid"

# Functions under test are taken from the script itself, with the globals they use set here.
extract_function() { sed -n "/^$1()/,/^}/p" "${HERE}/console"; }
load_functions() {
  APP_DIR="${HERE}/.."
  ENV_FILE="${RDSC_ENV_FILE}"
  log() { :; }
  for name in "$@"; do eval "$(extract_function "${name}")"; done
}

# 1. the home is created on first start: a git repository with the example configuration, committed,
#    with an upstream so the console's checkout check works, and nothing left uncommitted
(
  load_functions env_value home_dir ensure_home
  : > "${RDSC_ENV_FILE}"
  ensure_home
  ensure_home
)
[ -f "${RDSC_HOME_DIR}/config/environments.yaml" ] || { echo "the home has no configuration"; exit 1; }
[ -f "${RDSC_HOME_DIR}/config/terraform.yaml" ] || { echo "the home has no terraform settings"; exit 1; }
[ -z "$(git -C "${RDSC_HOME_DIR}" status --porcelain)" ] || { echo "the home has uncommitted files"; git -C "${RDSC_HOME_DIR}" status --short; exit 1; }
[ "$(git -C "${RDSC_HOME_DIR}" rev-parse --abbrev-ref '@{u}')" = "origin/main" ] || { echo "the home has no upstream"; exit 1; }
[ "$(git -C "${RDSC_HOME_DIR}" rev-list --count HEAD)" = "1" ] || { echo "second start must not add commits"; exit 1; }

# 2. an existing repository without configuration gets the example copied in, uncommitted, and its history is untouched
EXISTING="${SCRATCH}/existing"
git init -q -b main "${EXISTING}"
git -C "${EXISTING}" commit -q --allow-empty -m "Existing history"
(
  RDSC_HOME_DIR="${EXISTING}"
  load_functions env_value home_dir ensure_home
  ensure_home
)
[ -f "${EXISTING}/config/environments.yaml" ] || { echo "existing repository got no configuration"; exit 1; }
[ "$(git -C "${EXISTING}" rev-list --count HEAD)" = "1" ] || { echo "existing repository history changed"; exit 1; }

# 3. profiles: no env file content gives the bundled db and, with the example sandbox, the simulated account
: > "${RDSC_ENV_FILE}"
out="$("${HERE}/console" profiles | tr '\n' ' ')"
[[ "${out}" == *"bundled-db"* && "${out}" == *"simulated-aws"* ]] || { echo "expected both profiles, got: ${out}"; exit 1; }

# 4. a shared database url disables the bundled db profile
echo "RDSC_CONTROL_DATABASE_URL=postgresql+psycopg://u:p@h/d" > "${RDSC_ENV_FILE}"
out="$("${HERE}/console" profiles | tr '\n' ' ')"
[[ "${out}" != *"bundled-db"* && "${out}" == *"simulated-aws"* ]] || { echo "expected only simulated-aws, got: ${out}"; exit 1; }

# 5. the home location can come from the env file
echo "RDSC_HOME_DIR=${EXISTING}" > "${RDSC_ENV_FILE}"
(
  unset RDSC_HOME_DIR
  load_functions env_value home_dir
  [ "$(home_dir)" = "${EXISTING}" ] || { echo "home location was not read from the env file"; exit 1; }
)

# 6. the env file is written from git identity when missing
rm -f "${RDSC_ENV_FILE}"
(
  load_functions env_value home_dir write_env_file
  write_env_file
)
grep -q '^RDSC_OPERATOR_NAME=Test Operator' "${RDSC_ENV_FILE}" || { echo "env file missing operator name"; cat "${RDSC_ENV_FILE}"; exit 1; }

# 7. the compose file is valid with every profile
(cd "${HERE}/.." && RDSC_ENV_FILE="${RDSC_ENV_FILE}" docker compose -f compose.yaml --profile bundled-db --profile simulated-aws config -q)

# 8. values in the operator env file reach the compose file: shared database URL, custom port and the home
cat > "${RDSC_ENV_FILE}" <<EOF_ENV
RDSC_OPERATOR_NAME=Test Operator
RDSC_CONTROL_DATABASE_URL=postgresql+psycopg://u:p@shared-host:5432/rdscommander
RDSC_HTTP_PORT=9999
EOF_ENV
rendered="$(cd "${HERE}/.." && RDSC_ENV_FILE="${RDSC_ENV_FILE}" docker compose -f compose.yaml --env-file "${RDSC_ENV_FILE}" --profile simulated-aws config)"
echo "${rendered}" | grep -q 'RDSC_CONTROL_DATABASE_URL: postgresql+psycopg://u:p@shared-host:5432/rdscommander' || { echo "shared database URL did not reach the backend"; echo "${rendered}" | grep -n RDSC_CONTROL_DATABASE_URL; exit 1; }
echo "${rendered}" | grep -q 'published: "9999"' || { echo "custom port did not reach the published port"; echo "${rendered}" | grep -n -A2 'ports:'; exit 1; }
echo "${rendered}" | grep -q "source: ${RDSC_HOME_DIR}" || { echo "home is not mounted"; echo "${rendered}" | grep -n -B1 -A3 'target: /data'; exit 1; }
echo "${rendered}" | grep -q '/root/.ssh' || { echo "ssh directory is not mounted"; exit 1; }
echo "${rendered}" | grep -q '/root/.gitconfig' || { echo "git configuration is not mounted"; exit 1; }
echo "${rendered}" | grep -q 'hostname: rdscommander-console' || { echo "the backend has no fixed hostname"; exit 1; }
echo "console script tests passed"
