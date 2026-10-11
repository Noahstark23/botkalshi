# US500 V2.4 — identidad, costes y actividad de motores

11 de octubre de 2026. Base revisada: 80109bc10eba7abc099d1effe0c66790d4f22634. Cambios aislados en research/us500; M2/M5, main y ejecutores permanecen separados.

## Defectos reproducidos y corregidos

El replay directo comparaba ticker y unidades pero no market_id, market_version, exchange_index ni asset_class. Un archivo sintético con market_id cambiado antes del primer fill producía seis fills hipotéticos y calificaba el recorrido como completo. Ahora runner, escenario round-trip y ensamblador usan el mismo validador de identidad. Las fixtures sintéticas históricas pueden omitir el bloque completo; capturas declaradas observadas y metadata parcial exigen identidad completa. Una contradicción durante el replay pausa y conserva exposición. No migra ni reescribe bases históricas.

Un archivo ensamblado podía conservar funding=0 en session_interval_cost_evidence y ejecutar funding=100 en el frame; la reproducción devolvió PnL ficticio99.7414. Ahora importes, tiempos, nota y forma de la evidencia deben concordar con el frame. Metadata y hash del assembly deben concordar con todas las capturas. El rechazo sucede antes de crear/escribir una base. La revisión adversarial adicional reprodujo un KeyError con metadata ausente y hash de null; se agregó rechazo DataError y regresiones.

El reporte añade input_provenance: frames declarados sintéticos/observados, solicitados/procesados/con observación válida, rango y gap máximo de entrada. Origen auténtico, sesión completa e independencia permanecen explícitamente no verificados. Los frames procesados incluyen el frame que provocó bloqueo; no son muestras independientes. Un hash detecta cambios, no certifica origen. El protocolo EVALUACION_INDEPENDIENTE.md define lo que todavía debe congelarse/medirse antes de evaluar una nueva muestra.

## Telegram: actividad registrada de M6/REST/M8/M9

El API existente /stats/edges?days=1 permite consultar conteos de señales, sin escribir estado ni consultar mercados. Se conecta esa lectura al mismo proveedor y receptor. La ventana va desde el inicio UTC del día anterior hasta generated_at; no es una ventana móvil de24horas. Caché de300s tanto para éxitos como fallos, tiempo y tamaño limitados, allowlist y validación estricta de enteros/fechas/unidades.

Se muestran linemove, multi_outcome, ofi y spillover. No se muestran PnL, saldos, tickers, cuerpos crudos ni spillover_exec como prueba de ejecución. Cero filas sólo significa que la consulta no encontró registros en su ventana y retención; no prueba que el motor esté apagado ni que nunca haya emitido señales. Un fallo/vencimiento muestra actividad desconocida, y una falla de estadísticas no borra /status válido.

No hay nueva telemetría de tareas o configuración de esos cuatro motores. Esos campos siguen desconocidos. No se añadió activación real, escritor de flags, credenciales financieras ni consumidor Telegram adicional.

## Verificación y pendientes

La suite integrada offline y dos recorridos se conservan en evidence/V2_4/TESTS.log; Ruff y formato en el mismo directorio. MANIFIESTO_V2.json contiene conteo y hashes de esta versión. Tests cubren identidad antes/después del fill, exposición y reinicio, contradicciones antes de tocar DB, metadata ausente/malformada, actividad vacía/inválida/caducada y caché de fallos. El ejemplo válido sigue USD100→98.6643, pérdida ficticia1.3357.

La instalación/recepción conectada del panel y el SHA exacto se registran externamente en TELEGRAM_CLOUD_20261011/INFORME.md. Aprobar offline no acredita entrega Telegram. Quedan las sesiones abiertas, condiciones/fees/permisos específicos, evaluación económica independiente, conciliación privada y ejecutor real. Claude aún no había revisado estos cambios al prepararlos.
