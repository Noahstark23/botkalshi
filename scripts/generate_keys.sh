#!/bin/bash
# =============================================================
# Genera par de llaves RSA 4096 para autenticación con Kalshi.
#
# Uso:
#   bash scripts/generate_keys.sh
#
# Output:
#   config/kalshi_private_key.pem  (NUNCA committear, NUNCA compartir)
#   config/kalshi_public_key.pem   (subir a Kalshi en Settings → API Keys)
# =============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/config"

mkdir -p "$CONFIG_DIR"

PRIVATE_KEY="$CONFIG_DIR/kalshi_private_key.pem"
PUBLIC_KEY="$CONFIG_DIR/kalshi_public_key.pem"

if [ -f "$PRIVATE_KEY" ]; then
    echo "⚠️  Ya existe $PRIVATE_KEY"
    echo "    Sobrescribir invalidará la llave en Kalshi."
    read -p "¿Continuar? [y/N]: " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "Cancelado."
        exit 0
    fi
fi

echo "🔐 Generando llave privada RSA 4096 (puede tardar ~5 segundos)..."
openssl genrsa -out "$PRIVATE_KEY" 4096 2>/dev/null

echo "🔓 Extrayendo llave pública..."
openssl rsa -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY" 2>/dev/null

# Permisos seguros
chmod 600 "$PRIVATE_KEY"
chmod 644 "$PUBLIC_KEY"

echo ""
echo "✅ Llaves generadas correctamente."
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  PRÓXIMOS PASOS"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "1. Copia el contenido de la llave PÚBLICA:"
echo "   cat $PUBLIC_KEY"
echo ""
echo "2. Ve a https://kalshi.com → Settings → API Keys"
echo ""
echo "3. IMPORTANTE: Selecciona 'Demo Environment' (no Production)"
echo ""
echo "4. Click 'Add API Key' y pega la llave pública"
echo ""
echo "5. Kalshi te genera un Key ID (UUID). Cópialo."
echo ""
echo "6. Para deployment en Coolify:"
echo "   - El Key ID va como env var KALSHI_API_KEY_ID"
echo "   - La llave PRIVADA se monta como volume secret"
echo "   - Ver docs/DEPLOY_COOLIFY.md para detalles"
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  🔒 SEGURIDAD"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "  NUNCA commits $PRIVATE_KEY a Git"
echo "  NUNCA pegues su contenido en chats, issues, etc."
echo "  Si se compromete: regenera y rota inmediatamente"
echo ""
echo "═══════════════════════════════════════════════════════════"
