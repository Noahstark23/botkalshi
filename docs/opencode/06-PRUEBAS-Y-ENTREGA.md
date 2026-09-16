# 06 — Pruebas, secuencia y entrega

## Secuencia de trabajo

P0: inspección, baseline offline y regresiones de fecha/procedencia del guard; conservar elegibilidad real deshabilitada hasta conciliación verificable. P1: coherencia de paquetes, cursores y cobertura. P2: adapter read-only con mocks y ledger sintético. P3: evaluación simulada con límites completos y mapa de correlación. P4: exportación/lectura de evidencia. P5: aceptación remota sólo cuando exista acceso/permiso operativo específicos y se haya comparado el candidato pendiente.

No implementar P0-P5 de golpe ni mantener el proyecto indefinidamente en diseño: cada bloque entrega código acotado y evidencia. Esta sesión comienza por P0; el resto describe dependencias, no trabajos en segundo plano. M5 market-making y M3 salida direccional anticipada mantienen cohortes y criterios separados; no cambiar parámetros para hacer aprobar un experimento distinto. Los defectos históricos K01-K08 no se declaran reparados sin sus regresiones.

## Inspección antes de ejecutar

En el checkout autorizado registrar `git status --short`, `git rev-parse HEAD`, `git diff --stat`, `python3 --version` y, si está instalado, `opencode --version`. No imprimir remotes/configs con tokens o secretos. Revisar test discovery, imports, conftest, hooks y entrypoints; usar un entorno aislado sin variables de producción ni red externa. No ejecutar pytest raíz ni instalar el paquete completo sólo para probar estos módulos.

Después de revisar y aislar, comandos de prueba candidatos desde la raíz del repo:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s infra/digitalocean-shadow/tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 infra/digitalocean-shadow/test_collector.py
bash -n infra/digitalocean-shadow/install.sh
```

Estos comandos no proporcionan por sí solos aislamiento de red. Si el entorno no puede garantizarlo, no ejecutar con credenciales: documentar el bloqueo. Contar tests desde la salida real. Pruebas escritas, baseline histórico, tests del parche y CI son evidencias distintas. Este handoff no afirma que los comandos anteriores se hayan ejecutado.

## Matriz mínima de aceptación

| ID | Caso | Evidencia exigida |
|---|---|---|
| SEC-01 | POST/PUT/PATCH/DELETE o ruta no permitida por el cliente de cuenta | Denegado antes de firma y red; sin contactar Kalshi para probar la denegación. |
| SEC-02 | Redirect, host impostor, path codificado, URL libre, prompt injection | No puede ampliar permisos, acceder a secretos o ejecutar comandos. |
| SEC-03 | Import y flags de ejecución inesperados | Cero jobs/red; error controlado, no activación legacy. |
| RISK-01 | Fecha vacía, inválida, naive, futura o vencida | Sin RECONCILED ni elegibilidad real. |
| RISK-02 | Fuente arbitraria/hash sin ledger completo | Entrada declarada o pendiente, nunca saldo verificado. |
| RISK-03 | JSON duplicado, boolean, NaN, Infinity, enorme, subcentavo | Rechazo o conversión precisa documentada; jamás redondear riesgo hacia abajo. |
| RISK-04 | Dos contratos del mismo evento/tesis | Tope conjunto de una unidad, no uno por contrato. |
| RISK-05 | Ganancia/cierre y nuevo intento en el mismo día | No resta riesgo ya comprometido del presupuesto diario. |
| RISK-06 | Caída de capital, recuperación y reinicio | Unidad disminuye y no aumenta automáticamente. |
| RISK-07 | Pérdidas semanales -12 / acumuladas -20 | Pausa persistente; sólo revisión documentada permite reconsiderar. |
| RISK-08 | P=-18.50 y riesgo abierto=1.50, fixture simulado | Nueva propuesta real no elegible; sin doble contar P&L no realizado. |
| RISK-09 | Cash reservado, orden parcial, cancelación/timeout UNKNOWN | Sin liberar efectivo/riesgo por falta de confirmación. |
| DATA-01 | Cursor agotado, cap, A-A, A-B-A, página errónea | Cobertura exacta, parcial/ERROR si corresponde, sin bucle infinito. |
| DATA-02 | Partido iniciado pero contrato open / close_time futuro | No se etiqueta prepartido por estado de contrato. |
| DATA-03 | Una categoría sin proveedor | Sigue visible como pendiente/no revisada, no sin eventos/valor. |
| DATA-04 | Paquete/health/coverage/risk de distintos ciclos o viejo | Consumidor rechaza, aunque el proceso esté vivo. |
| DATA-05 | Libro viejo/cache, vacío, mal ordenado, sin fee/payout | No precio ejecutable ni sizing real; conserva hora original. |
| LEDGER-01 | Fill repetido entre páginas/históricos/reinicio | Exactamente una contabilización con trazabilidad. |
| LEDGER-02 | Fills, venta parcial, cobro y corrección | Coste y remanente reconciliados; fees una vez. |
| LEDGER-03 | Aportes/retiros y posiciones previas a apertura | No retornos falsos ni inclusión en la cohorte nueva. |
| LEDGER-04 | Actividad concurrente o ventana histórica incompleta | PARTIAL/UNKNOWN hasta resolver, no curva reconstruida. |
| SIZING-01 | Ask/depth y comisiones redondeadas por orden | Mayor entero n dentro de TODOS los límites; coste mínimo imposible => descartar. |
| BRIDGE-01 | Paquete válido y otro stale recibido por el consumidor | Verificación ID/hash/edad y rechazo efectivo, no comunicación supuesta. |

## Entrega técnica por bloque

Mostrar SHA antes/después, archivos modificados, versiones, comandos exactos, tests pasados/fallidos/omitidos, aislamiento aplicado y limitaciones. Una regresión debe fallar contra el defecto y pasar contra el fix; no borrar o relajar el test para declarar éxito. No editar workflows/branch protections ni hacer merge automático. Un PR en draft o mergeable no acredita CI ni producción.

Actualizar un handoff de sesión con: objetivo, evidencia, cambios, pruebas, estado de datos/cuenta/puente, bloqueos y una sola próxima tarea. Marcar propuestas como propuestas. No incluir logs privados o credenciales; limitar cambios a archivos revisados, no `git add .` indiscriminado.

## Puerta operativa futura, no orden de despliegue

Confirmar el host dedicado, identidad SSH, estado del servicio, SHA/rutas reales y cambios locales antes de cualquier acción. Resolver comparación con prdry.tgz sin saltarse bloqueos. Conservar releases/datos y respaldo verificable cuando proceda; revisar cambios de schema y reversibilidad. Preparar plan por SHA exacto con aceptación de un ciclo nuevo/coherente y rollback documentado. No ejecutar un instalador basándose en una ruta supuesta de una conversación.

La aceptación del observer no autoriza lecturas privadas; la del reader no autoriza trading. Configuración de credenciales, nuevos costes o cambios operativos requieren su propia autorización. No crear VPS, reiniciar, apagar, borrar, modificar firewall, instalar agentes remotos o programar cron desde este handoff.
