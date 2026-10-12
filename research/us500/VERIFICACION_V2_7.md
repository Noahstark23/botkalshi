# Verificación V2.7 — 12 de octubre de2026

Base de revisión: dff81f919dede937e8965de98a0e6bb12ebad90f. M2/M5 y main separados.
Resultados reproducibles en `evidence/V2_7/`; evidencias anteriores conservadas.

## Resultados locales

456 pruebas y dos recorridos con `verify_offline.py`, Python3.14, red bloqueada.
Ruff0.14.0 y formato revisados. El caso histórico mantiene100→98.6643USD,
reinicio idéntico, cero órdenes reales y permiso paper final falso. La matriz
CI y despliegue se informan separadamente por SHA; este documento no los presume.

Se reprodujeron TG-01/03/04/05 contra un archivo limpio de la base dff81f9:
update grande detenía cursor en0;429 era rechazo permanente; retroceso de reloj
no entraba en recuperación; health503 resultaba fresh=false. Las nuevas pruebas
cubren esas rutas y rechazos malformados, confirmaciones, cursor y falta de reenvío.

| Hallazgo | Corrección y alcance |
|---|---|
| TG-01 | Consume ID/hash de update excesivo sin contenido, acción ni respuesta; conserva conflicto de ID y límite global |
| TG-03 | Espera retry_after acotado y recupera recepción; envío explícitamente rechazado queda FAILED, nunca reenviado |
| TG-04 | Recuperación pausada hasta alcanzar tiempo aceptado; no baja timestamps ni acepta nonce vencido |
| TG-05 | Parser503 limitado a health/detail/unhealthy válido; pausa visible, estados no publicados siguen desconocidos |
| TG-02/endurecimiento | Caché saneada de usuario separado y denegación IP del panel; efectividad de BPF se verifica en host antes de dar cierre operativo |
| F2 | Settlement positivo menor de1e9 en runner/ledger; datos excesivos rechazados antes de creación o mutación contable |
| CI-01 | Manifiesto exige inventario exacto además de hashes y rechaza symlinks; excluye únicamente manifiesto propio y caches Python/Ruff/pytest |
| DOC-01 | Tasas sin tarifario archivado pasan a supuestos de ejemplo; no se afirma tarifa vigente |
| F1 | Se conserva bloqueo deliberado ante UNKNOWN; no se repiten slots ni se fuerza continuidad |

TG-06/07/08 fueron resumidos por Claude junto con endurecimiento y documentación;
no se asignan cierres individuales sin recibir sus descripciones completas.
Las restricciones de integración futura no autorizan controles reales por chat.

## Evaluación larga

Límite8.192 frames, ensamblador1–8 segmentos, una sola DB/configuración/estrategia.
Caso sintético7.141 frames/119h, costes y funding explícitos en los bordes,
posición conservada y reinicio idéntico. Prueba de interrupción al comenzar tramo;
rechazo de cambios de historia, huecos excesivos, identidad distinta, solapamiento,
coste faltante y exceso de tamaño. Ver `EVALUACION_LARGA.md`.

El ensayo técnico no evalúa la captura en curso. Pendientes: cierre y auditoría
integral, costes/funding y condiciones de cuenta respaldados, protocolo congelado,
evaluación independiente. M6/REST/M8/M9 aún necesitan instrumentación publicada
por runtime para sus flags/tareas; el panel no los deduce de conteos.
