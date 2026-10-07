# Perfil50: preparación local

El titular asigna un máximo de US$50 para evaluar el bot. Este cambio añade un
perfil opt-in independiente de Bank200. No inicia procesos, coloca órdenes,
carga credenciales ni modifica servicios o bases históricas. La plantilla
`infra/pilot-bank50/review.env.example` mantiene todos los flags de órdenes
apagados, el reconocimiento de capital en cero y el inicio sin definir.

## Controles implementados

| Control | Comportamiento del perfil50 |
|---|---|
| Capital | Referencia y techo US$50; con cash50 y factor90, efectivo45 |
| Piso | Efectivo mínimo40,50; cash inferior45 bloquea entradas |
| Saldo | Confirmado, antigüedad máxima60s; refresco no superior al TTL |
| Operación/evento | Hasta US$1 de compromiso calculado; permite dimensionar un par entero si existe oportunidad válida |
| Abierto / riesgo nuevo diario | Hasta US$3, incluidos los fees calculados en la reserva |
| Historial completo | Hasta5.000c entre riesgo original y reserva calculada de fees de BUY en TODA la DB |
| PnL realizado del ledger | Pausa durable a −US$3 semanal o −US$5 acumulado; siguen los stops generales más estrictos |
| Datos desconocidos | Settlement sin fecha/PnL, entrada sin compromiso original o cash inválido bloquean |
| Motores | Sólo M1 puede abrir entradas con este perfil; no habilita M2, REST, M5 ni gestores adicionales |

El perfil no cambia Bank200. Sus límites por operación/día se calculan mediante
una política separada; aplicar directamente el sizing de1% de Bank200 a US$50
impediría dimensionar un par normal de contratos enteros.

La reserva revalida el máximo histórico dentro del mismo `BEGIN IMMEDIATE` que
persiste los intents. Cancelaciones, fills parciales, splits, errores, cambios
de fecha de inicio, rollover y reinicios no reintegran ese presupuesto. Los
compromisos de otros perfiles anteriores también cuentan. Una DB antigua sin
compromisos verificables bloquea: no borrarla ni reconstruirla desde cantidades
mutables para lograr admisión. Conservar la identidad y el historial de la DB.

## Alcance y pendientes

El contador de US$50 limita compromisos BUY y la reserva de comisiones calculada
por el código. **No certifica una pérdida máxima de cuenta de US$50.** M1 todavía
utiliza el multiplicador predeterminado de fees y no reserva separadamente todos
los costes posibles de rollback. Antes de un piloto real falta verificar la
tarifa efectiva de los mercados autorizados y conciliar costes observados. El
PnL del ledger usa contabilidad legacy; los stops no son MTM ni liquidaciones
garantizadas. El acumulado50 suma el PnL neto de toda la DB: ganancias históricas
pueden compensar pérdidas posteriores. No representa una cohorte nueva aislada.
El perfil no acredita rentabilidad.

No hay pruebas conectadas de este perfil50 ni despliegue. Las pruebas offline
ejercitan configuración, sizing, DB temporal, límites concurrentes y pausas
durables; ninguna operación se envía a una cuenta. El expediente IOC Demo anterior
sigue sin resolución específica y su reserva ficticia permanece separada.

Para evaluar una activación, el titular debe revisar versión, configuración,
cuenta/capital real y estos pendientes. El fondeo y la activación financiera
quedan a cargo del titular. Este documento y los tests no los ejecutan.
