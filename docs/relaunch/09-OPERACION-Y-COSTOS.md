# 09. Operación, continuidad y costos

## Runbook de una sesión de laboratorio

Verificar versión, perfil, reloj, espacio y evidencia de que no hay writer duplicado. Leer último estado clean/invalid y último backup probado. Cargar únicamente fuentes y símbolos permitidos. Durante captura medir heartbeat, quote_age, gaps WS, errores/429, profundidad válida, retraso de escritor y cuota de proveedor. Al cerrar, persistir métricas/cohorte y publicar exportación saneada con cobertura y pendientes.

Ante fallo de fuente: estado DATA_INSUFFICIENT; no usar precio congelado como vigente. Ante caída de IA: reporte determinista sin nueva evaluación IA. Ante disk-full: pausar ingestión/evaluación y preservar estado; no borrar ledger para reanudar. Ante drift de reglas/fees: nueva evidencia y cuarentena de cálculos afectados. Ninguno de estos fallos debe activar otro motor.

Objetivos propuestos, no SLA medidos: detectar ausencia de heartbeat en 60 segundos; backup cada sesión y al menos diario cuando haya captura continua; RPO de observaciones <=24 horas y RTO local <=60 minutos para laboratorio. Si algún día hubiera dinero, estos objetivos deben revisarse con ledger de fills y reconciliación; no son garantías para ejecución real.

## SQLite y backups

Usar API de backup de SQLite, no copiar solamente `trades.db` mientras WAL esté activo. Escribir copia a destino nuevo, comprobar integrity_check, versión/esquema, hashes y recuentos; ensayar restauración sobre ubicación separada. Readers de snapshots con `mode=ro`. No aplicar immutable=true a una DB viva que cambia. Backups cifrados y con retención propuesta de siete diarios/cuatro semanales; no versionar bases reales en GitHub.

## Presupuesto

Autorizado por esta entrega: **cero nuevas contrataciones y cero consumo automático de API**. Arranque offline/local, usando hardware y herramientas ya disponibles. Esto no significa costo económico cero: electricidad, almacenamiento, conexión, tiempo y suscripciones compartidas deben medirse.

La API de IA se factura aparte de ChatGPT. Propuesta futura: IA <=USD 5/mes, resto de proveedores nuevos apagados hasta aprobación explícita con tarifa y cuota. Si datos de sportsbooks requieren plan de pago, medir llamadas y derechos de uso antes de activarlo; no sustituir por scraping que eluda límites.

Mostrar dos cuentas: costo incremental del proyecto y costo total asignado (incluida la parte definida de herramientas compartidas). No atribuir una suscripción completa a trading para un cálculo y omitirla para otro. Alertas del proveedor no son hard cap: reservar consumo máximo, limitar concurrencia/retries y bloquear nuevas solicitudes al agotarlo. No afirmar que ese control exista por escribirlo aquí.

## Aspiración USD 40/mes

Con capital de referencia 200, 40 son 20% mensual antes de costos e impuestos. Si costos fijos adicionales fueran 10, necesitaríamos 50 de beneficio de trading después de costos variables: 25% de ese capital. Con 80 fijos, serían 120, o 60%. Son identidades aritméticas, no retornos esperados ni recomendación de aumentar capital.

No forzar operaciones para alcanzar 40. Reportar ingresos/cobros brutos, P&L neto de trading, costos de infraestructura y resultado del proyecto por separado. Mantener ingreso futuro presupuestado en cero mientras no esté demostrado. No usar el experimento como respaldo de Internet, renta, vivienda o deuda.

## Continuidad desde otro país

Antes de cualquier acceso desde Nicaragua, verificar elegibilidad territorial vigente, términos de Kalshi y proveedores, residencia fiscal y capacidad legal de mantener la cuenta. Tener SSN no demuestra elegibilidad desde cualquier jurisdicción. No usar VPN, dirección ajena o cuentas de terceros para eludir restricciones. La demo/replay de ingeniería puede continuar sin dinero aunque la operación real no esté permitida.
