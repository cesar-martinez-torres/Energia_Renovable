#!/usr/bin/env bash

set -euo pipefail

readonly REQUIRED_MAJOR="3"
readonly REQUIRED_MINOR="13"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ENVIRONMENT_DIR="${SCRIPT_DIR}/.venv"
readonly REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements.txt"
readonly KERNEL_NAME="energia-renovable-2026"
readonly KERNEL_DISPLAY_NAME="Python (Energia Renovable 2026)"

find_course_python() {
    local candidate
    local version

    for candidate in python3.13 python3; do
        if ! command -v "${candidate}" >/dev/null 2>&1; then
            continue
        fi

        version="$("${candidate}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
        if [[ "${version}" == "${REQUIRED_MAJOR}.${REQUIRED_MINOR}" ]]; then
            command -v "${candidate}"
            return 0
        fi
    done

    return 1
}

if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
    echo "Error: no se encontro requirements.txt junto a setup_mac.sh." >&2
    exit 1
fi

if ! COURSE_PYTHON="$(find_course_python)"; then
    echo "Error: no se encontro Python 3.13." >&2
    echo "Instalalo desde https://www.python.org/downloads/macos/ y vuelve a ejecutar este archivo." >&2
    exit 1
fi

echo "Python 3.13 encontrado en: ${COURSE_PYTHON}"

if [[ ! -d "${ENVIRONMENT_DIR}" ]]; then
    echo "Creando el ambiente virtual .venv..."
    "${COURSE_PYTHON}" -m venv "${ENVIRONMENT_DIR}"
else
    echo "El ambiente virtual .venv ya existe; se actualizara."
fi

readonly ENVIRONMENT_PYTHON="${ENVIRONMENT_DIR}/bin/python"

if [[ ! -x "${ENVIRONMENT_PYTHON}" ]]; then
    echo "Error: el ambiente virtual no contiene bin/python." >&2
    echo "Mueve .venv a la Papelera y vuelve a ejecutar este archivo." >&2
    exit 1
fi

echo "Actualizando las herramientas de instalacion..."
"${ENVIRONMENT_PYTHON}" -m pip install --upgrade pip setuptools wheel

echo "Instalando las herramientas del curso..."
"${ENVIRONMENT_PYTHON}" -m pip install --upgrade -r "${REQUIREMENTS_FILE}"

echo "Registrando el kernel de Jupyter..."
"${ENVIRONMENT_PYTHON}" -m ipykernel install \
    --user \
    --name "${KERNEL_NAME}" \
    --display-name "${KERNEL_DISPLAY_NAME}"

echo "Verificando las bibliotecas principales..."
"${ENVIRONMENT_PYTHON}" -c "import matplotlib, numpy, numpy_financial, openpyxl, pandas, pvlib, requests, scipy; print('Entorno verificado correctamente.')"

echo
echo "Instalacion terminada."
echo "Kernel: ${KERNEL_DISPLAY_NAME}"
echo "Para comprobar Python, ejecuta:"
echo "./.venv/bin/python ./00_verificar_entorno.py"
echo "Para iniciar Jupyter Lab, ejecuta:"
echo "./.venv/bin/python -m jupyter lab"

