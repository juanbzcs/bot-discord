#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "❌ No se encontró $PYTHON_BIN en el sistema."
  exit 1
fi

echo "📦 Creando entorno virtual en $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "⬆️  Actualizando pip"
python -m pip install --upgrade pip

echo "📚 Instalando dependencias"
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "📝 Se creó .env desde .env.example"
fi

echo
echo "✅ Instalación completada."
echo "Siguientes pasos:"
echo "1) Edita .env y coloca tu DISCORD_TOKEN real"
echo "2) Activa el entorno: source $VENV_DIR/bin/activate"
echo "3) Ejecuta el bot: python bot.py"
