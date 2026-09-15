# DigitalOcean shadow collector

Servicio autónomo de **investigación**, no de ejecución. Lee mercados públicos de Kalshi y opcionalmente The Odds API, guarda snapshots SQLite y exporta `packets/latest.json` para revisión externa/IA.

## Seguridad

- Rechaza claves privadas/API de Kalshi y flags de ejecución.
- No implementa crear/cancelar órdenes, posiciones, balances ni depósitos.
- No abre puertos; UFW deja solo SSH.
- Corre como usuario `botkalshi` con hardening de systemd.
- `ODDS_API_KEY` es opcional y debe configurarse solo en `/etc/botkalshi-research.env` del servidor, nunca en GitHub o chat.

## Despliegue

En un Ubuntu 24.04 nuevo, clonar la rama `feat/digitalocean-shadow-20260915` y ejecutar como root:

```bash
bash /opt/botkalshi/infra/digitalocean-shadow/install.sh
```

El instalador crea `/var/lib/botkalshi-research`, instala y activa `botkalshi-research.service`, y valida que `health.json` indique `SHADOW_READONLY` y `execution_authorized=false`.

Comprobación:

```bash
systemctl status botkalshi-research
journalctl -u botkalshi-research -n 100 --no-pager
cat /var/lib/botkalshi-research/health.json
```

Sin `ODDS_API_KEY`, Kalshi público sigue capturándose pero `sportsbook.status` queda `NOT_CONFIGURED`. Eso es un estado válido, no se inventa M2.

## IA

El paquete JSON es el punto de integración. Esta versión **no llama automáticamente a OpenAI** ni usa la suscripción de ChatGPT como API. Un worker de IA se añadirá aparte, con Structured Outputs y presupuesto propio, después de validar captura, frescura y matching. Ninguna salida IA tendrá permiso de ejecución.
