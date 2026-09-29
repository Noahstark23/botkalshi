# Arranque local de botkalshi — Mac mini

Implementación de infraestructura inicial, separada de `src.runner`. **No arranca M2/M5, no los reescribe ni repara K01–K08.** Reutiliza el repositorio y permite inspeccionar un snapshot de sus tablas M5 sin tocar el ledger. Este bootstrap no es un bot de trading ni una cohorte F1.

## Qué funciona en el paquete

- Servicio OFFLINE con heartbeat, salida limpia y fixture inequívocamente SINTÉTICA.
- SQLite local `bootstrap.sqlite3` para metadatos/reportes; NO es `trades.db` ni contiene saldos.
- Inspector de las tablas existentes `mm_shadow_fills` y `mm_experiment_runs`, modo SQLite de lectura, conteos sin datos personales; no evalúa el gate económico.
- Reportes JSON/MD exportables para revisión interactiva; no hay API de IA ni MCP desplegado.
- Backup consistente de la DB del bootstrap y comprobación de integridad. Copia en el mismo volumen: todavía NO protege frente a pérdida del disco/equipo.
- Captura pública Kalshi opcional, una sola vez: una serie, máximo cinco mercados y siete GET; ni siquiera se declara barrido completo de MLB. No usa The Odds API, claves o cuotas pagadas.
- Compose aislado: sin red por defecto, rootfs de lectura, usuario no root, sin puertos, sin socket Docker, sin .env/secretos montados, límites y logs rotados.

## Arranque en el Mac (requiere Docker Desktop ya disponible)

Desde esta rama/copia, sin mezclar con el compose raíz:

```bash
bash infra/mac-lab/lab.sh doctor
bash infra/mac-lab/lab.sh test
bash infra/mac-lab/lab.sh up
bash infra/mac-lab/lab.sh status
bash infra/mac-lab/lab.sh export
```

El script rechaza contexto Docker remoto. No instala Docker ni usa sudo. La primera preparación descarga la imagen OFICIAL `python:3.12-slim-bookworm` y resuelve/guarda su digest en `.local/build.env`; las construcciones posteriores usan ese digest. No se declara un digest verificado antes de ejecutarlo en tu equipo. El build necesita Internet para la imagen; **OFFLINE se refiere al contenedor en ejecución**, no a esa descarga.

No se instala el pyproject legacy: el bootstrap usa únicamente biblioteca estándar. El contexto de build incluye solo este directorio y excluye todo salvo runtime, Dockerfile y tests. En la imagen no existen el runner, clientes autenticados o ejecutores antiguos. No hay mecanismo para elevar a LIVE con un flag; se rechazan flags de ejecución y credenciales reconocidas.

`up` mantiene un servicio de prueba: crea un reporte sintético al arrancar y actualiza su heartbeat; **no escanea, no inventa nuevos fills y no analiza partidos**. Ante un reinicio, el nuevo reporte sigue marcado SINTÉTICO. `status` falla si está parado o el heartbeat caducó.

Exportación: `infra/mac-lab/.local/reports/latest.json` y `latest.md`. No publicar snapshots reales ni esa carpeta en GitHub. No hay dashboard web expuesto. `stop` solo detiene este proyecto, no borra volúmenes ni altera Nortex.

## Datos de los motores antiguos

Tras disponer de un snapshot consistente y autorizado (no copiar solo una DB viva con WAL):

```bash
bash infra/mac-lab/lab.sh inspect /ruta/al/snapshot.sqlite3
```

El script detiene únicamente el servicio de laboratorio para respetar un escritor; monta ese archivo readonly. No monta home ni el directorio padre. Rechaza archivos inexistentes, enlaces, WAL/journal adjunto y rutas ambiguas; **no crea una DB sustituta**. Si faltan tablas, `rows=null`, no cero. No lee filas financieras, no cambia criterios F1 y no ejecuta `tablero_gate.py`: eso sigue como paso posterior con el entorno legacy aislado.

## Consulta pública opcional (no automática al arrancar)

```bash
bash infra/mac-lab/lab.sh public-once
```

Este comando detiene el servicio OFFLINE y aplica solamente `compose.public.yaml`. Hace GET a un host fijo de Kalshi, sin proxies heredados ni redirects, con timeout, respuesta acotada, máximo siete llamadas y sin retries. No firma peticiones, no consulta cuentas, no crea/cancela órdenes. El modo bridge permite red saliente: **no es un firewall de dominio a nivel de kernel**; la restricción de rutas/origen pertenece al cliente de este paquete. No ejecutar código no confiable dentro de ese contenedor.

No calcula asks implícitos, fees efectivos, EV ni probabilidad; conserva los niveles observados y los marca no analizados. Si falla una llamada, guarda `INCOMPLETE` y retorna error. Si hay más páginas, lo indica y no reclama cobertura completa. Volver a `up` retorna al servicio OFFLINE sintético, no lo convierte en captura continua.

## Backup y parada

```bash
bash infra/mac-lab/lab.sh backup
bash infra/mac-lab/lab.sh stop
```

`backup` detiene el servicio local y crea una copia nueva mediante la API SQLite bajo `/data/backups`. No sobreescribe archivos existentes y elimina solo el destino nuevo si falla. El volumen persiste; la exportación cifrada a otro medio y retención quedan pendientes. No usar `down -v` ni `docker system prune` como mantenimiento.

## Pruebas y límites de esta entrega

```bash
python3.12 -m unittest discover -s infra/mac-lab/tests -v
```

Las pruebas bloquean conexiones TCP y emplean fixtures/mocks. Verifican estados y backups, no la compatibilidad en vivo del endpoint ni la ejecución del bot. Ver `VALIDACION.md` para los resultados realmente obtenidos, Python usado y verificaciones pendientes.

Para completar una instalación directamente desde ChatGPT se necesita conexión autorizada al Mac (Remote Desktop Commander propuesto). Una publicación de código o un PR NO equivale a instalar o arrancar Docker en ese equipo.

## Fuentes contrastadas 2026-09-14

- https://docs.kalshi.com/getting_started/quick_start_market_data
- https://docs.docker.com/reference/compose-file/services/
- https://sqlite.org/backup.html

Mantener apagadas las APIs de pago, claves y motores hasta completar aislamiento, dependencias, compatibilidad y revisión del plan del PR #252. No se han cambiado flags, políticas financieras, CI, permisos ni servicios existentes.
