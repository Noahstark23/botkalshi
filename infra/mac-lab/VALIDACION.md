# Validación del bootstrap — 2026-09-14

## Ejecutado en el entorno de trabajo del asistente

- Python 3.13.5 sobre Linux: **27 tests de unittest pasaron**, con conexiones TCP bloqueadas y proveedores simulados.
- Proceso real del runtime OFFLINE: arrancó, escribió reporte sintético, respondió por CLI al control de heartbeat, recibió SIGTERM y terminó con código 0.
- Backup mediante SQLite y lectura de la copia: contenido coincidente e integridad correcta.
- `bash -n infra/mac-lab/lab.sh`: sin errores de sintaxis.
- YAML leído con PyYAML y nueve comprobaciones estáticas de configuración: sin red por defecto, usuario no root, rootfs readonly, capacidades eliminadas, sin reinicio automático, sin puertos/secretos, volumen acotado y contexto de build local.

Estas comprobaciones son de ESTE bootstrap; no son los 1.675 tests históricos del bot ni demuestran que sus defectos K01–K08 estén reparados.

## No ejecutado

No hay Docker/Podman en este entorno. Por tanto **no se construyó imagen, no se ejecutó `docker compose config` real ni un contenedor**, no se probó Apple Silicon y no se ejecutó la suite bajo Python 3.12. La imagen/digest se resolverá en el equipo de destino, no se ha inventado un digest.

No se ha instalado nada en el Mac mini. Remote Desktop Commander fue propuesto pero todavía no se dispone de una sesión autorizada al equipo. No se han inspeccionado snapshots reales, usado claves, llamado The Odds API/OpenAI API, operado cuentas ni contratado servicios. Las llamadas a Kalshi se probaron con mocks; la compatibilidad viva del endpoint permanece pendiente.

## Puerta de instalación

En el Mac: `doctor`, `test`, `up` y `status` usando exclusivamente `infra/mac-lab/lab.sh`. Solo se considerará arrancado allí cuando Docker indique contenedor sano y el heartbeat local sea reciente. El modo inicial es OFFLINE con fixture, no M5 funcionando ni un radar completo. Después se podrá ensayar `public-once` explícitamente; no sustituye la revisión del entorno legacy antes de M2/M5.
