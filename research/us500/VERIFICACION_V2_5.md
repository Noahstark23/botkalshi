# US500 V2.5 — ventanas públicas y estados individuales

11 de octubre de 2026. Base: `74c7de66e8601f950b9b3d6716803fc32fef1ba7`.
411 pruebas y dos recorridos con sockets/DNS bloqueados pasan. Ruff0.14.0 limpio,
35 archivos Python formateados. Evidencias en `evidence/V2_5/` y hashes en
`MANIFIESTO_V2.json`. CI, instalación y progreso observado se verifican aparte.

El nuevo recolector tiene plan inmutable, fecha límite, cadencia60s, hasta26h,
presupuesto reservado antes de red y límite256MiB. No mantiene transacción durante
HTTP. Flock excluye otro proceso; reinicio conserva slots interrumpidos sin repetir.
Mercado cerrado termina; muestra inválida queda archivada, nunca fabricada.
Pérdida o cambio de un archivo pausa; regresión del reloj y huecos son explícitos.
status.json se publica atómicamente bajo el mismo lock. El servicio aislado limita
CPU/memoria y duración25s por invocación; no instala controles financieros.

La revisión independiente reprodujo dos bordes y verificó su corrección:
metadatos inválidos no fijan identidad de referencia y un archivo parcial perdido
invalida la integridad antes de seguir consultando. 24 tests propios del recolector
están incluidos en los411. Cobertura de slots no acredita historia continua,
autenticidad certificada ni sesión completa del mercado.

El contrato opcional `motor_telemetry` separa configuración, entradas/salidas,
instancia, tarea, callback y retorno. Incluye22 tests; el panel añade6 regresiones
de compatibilidad, datos ausentes/vencidos/malformados, aislamiento de cambios y
ausencia de controles reales. Runtime viejo válido sigue legible. La telemetría
ausente o rechazada no revive estados anteriores ni se convierte en apagado.

`TELEMETRIA_PENDIENTE.md` registra hashes y hooks del runtime83075. **No se instalaron
esos hooks**: la conexión completa requiere instrumentación posterior del runtime
financiero. El panel puede consumir el contrato cuando exista; no inventa los
estados faltantes. M2 conserva poll/salidas separados; M6/REST/M8/M9 son callbacks.

Sin mezcla M2/M5/main, órdenes, claves financieras, ni consumo Odds/Jev. Se conserva
el ejemplo sintético USD100→98.6643, pérdida ficticia1.3357. Siguen pendientes datos
completos verificados, costes/términos/permisos aplicables, evaluación independiente,
conciliación de cuenta y ejecutor real. Ni tests ni recopilación prueban rentabilidad.

## Seguimiento V2.5.1

Se corrigió el diagnóstico cuando /health o /status no responde: ahora dice
«sin observación», sin afirmar que el runtime carece de instrumentación. La
lectura válida sin bloque sigue diciendo que todavía no lo publica. Se agregó
regresión de esa diferencia; 412 pruebas y ambos recorridos pasan, con Ruff y
formato limpios en evidence/V2_5_1/. El código del recolector no cambió.
