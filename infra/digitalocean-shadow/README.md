# DigitalOcean: colector y verificación de investigación

No contiene órdenes ni integra todavía M2/M5. `collector.py` captura una serie de mercados públicos y produce paquetes; no es un barrido multiactivo completo. `reporting.py` crea borradores y `cycle_verifier.py` comprueba que health, packet, coverage y risk pertenecen al mismo ciclo.

`assistant_bridge.py` es el canal acotado para un evaluador de IA. Puede leer un ciclo verificado, escribir una evaluación estructurada y activar una pausa defensiva. No recibe claves de Kalshi, no tiene herramientas de orden, no puede reanudarse solo y no modifica flags de trading. Ver [puente del asistente](ASSISTANT_BRIDGE.md).

## Estado y documentación

Leer [memoria operativa](../../docs/operations/BOT_MEMORY.md), [runbook](../../docs/operations/RUNBOOK.md), [integraciones](../../docs/operations/INTEGRATIONS.md) y [contenido](../../docs/operations/CONTENT.md).

VM activa no demuestra proceso instalado. Este paquete no se ha desplegado remotamente desde la sesión que lo generó; falta acceso a terminal. Los tests locales no sustituyen verificación en Ubuntu/systemd.

## Instalación revisada

El comando antiguo sin argumentos queda sustituido. Desde un checkout revisado, en la consola del Droplet dedicado Ubuntu 24.04:

```bash
REV="$(git rev-parse HEAD)"
bash infra/digitalocean-shadow/install.sh "$REV" --dedicated-research-host
```

El instalador usa `/opt/botkalshi-research/releases/$REV`, conserva directorios existentes, ejecuta tests y requiere nueva captura después del arranque. Si no se verifica en 180 segundos, detiene el servicio sin borrar datos. No configura firewall/SSH ni añade claves. APIs pagadas requieren revisión aparte y no se habilitan por esta instalación.

Para verificar y producir borradores: seguir RUNBOOK.md. No usar el compose/runner legacy ni copiar claves históricas.

## Qué falta

Matching, reglas, fees, quotes ejecutables, fuentes independientes, timestamps por libro, presupuesto persistente de proveedores y evaluación del experimento. Un reporte `CAPTURE_VERIFIED_LOCAL` confirma solamente esos archivos, no rentabilidad ni integración IA. Un paquete o borrador no equivale a mensaje enviado, canal creado o video publicado.

El lector de cuenta está separado en `scripts/read_research_account.py`. Sólo ejecuta GET de balance, posiciones y fills, y produce evidencia saneada con `reconciled=false`; no habilita dinero real. Integrar un ledger conciliado, reservas, posiciones UNKNOWN y pausas persistentes sigue siendo requisito antes de cualquier discusión de ejecución.
