# Jev: investigación y simulación inicial

Fecha de evaluación: 6 de octubre de 2026, America/Los_Angeles.

**Estado: banco de pruebas local preparado. Evaluación real de Jev pendiente de cuenta y clave. No habilita operación real.** El usuario confirmó que aún no tiene cuenta de TypeSafe AI. La consola oficial mostró un bloqueo de Cloudflare en este navegador. No se crearon credenciales, suscripciones ni cuentas.

## Qué se verificó de Jev

Jev es el modelo de TypeSafe AI para evaluaciones estructuradas. Su primitiva `noul` devuelve una probabilidad de sí, entre 0 y 1. Ese valor responde a la pregunta enviada; no representa automáticamente la probabilidad de ganar una operación. El adaptador fija `jev-1.13.0`, valida el modelo devuelto y registra el hash de la solicitud. [Contrato oficial](https://docs.typesafe.ai/api).

No se encontró una garantía contractual de determinismo. El ejemplo oficial de consistencia repite una evaluación quince veces cambiando un identificador irrelevante; una respuesta varía entre 0,43 y 0,53. Eso puede cruzar un umbral binario, pero no demuestra variación con una entrada idéntica byte por byte. Nuestra prueba inicial hace dos repeticiones idénticas; será una comprobación mínima, no una demostración de consistencia. [Ejemplo del proveedor](https://docs.typesafe.ai/cookbooks/consistency_noul_cookbook).

El proveedor reconoce limitaciones en aritmética, fechas, ruido e instrucciones dentro de los datos. Por eso precios, comisiones, cantidades y controles permanecen en código. Jev sólo evalúa si un texto ficticio apoya una hipótesis de tendencia; no administra capital. [Limitaciones oficiales](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

La tarifa publicada es US$0,042 por millón de tokens de entrada; la salida es gratuita. El informe calcula coste estimado desde el uso devuelto. Esto no confirma condiciones de crédito o facturación de una cuenta futura. [Modelo y tarifa](https://docs.typesafe.ai/models).

## Implementación entregada

- `scripts/jev_client.py`: endpoint oficial fijo, clave explícita, sin redirecciones ni proxies del entorno, validación estricta y errores sin contenido sensible. Límite de 16 KiB por solicitud y 64 KiB por respuesta; sin reintentos automáticos. Timeout de HTTPX de diez segundos por fase/inactividad, con presupuesto de treinta segundos comprobado entre bloques: una lectura bloqueante puede exceder ese presupuesto hasta su timeout.
- `scripts/jev_simulation.py`: modo `offline` por defecto y modo `api` explícito. Máximo de treinta llamadas por ejecución. No carga `.env`, config, runner, credenciales de Kalshi ni DB. No importa clientes de órdenes; un test protege esa separación.
- `tests/scripts/test_jev_client.py` y `tests/scripts/test_jev_simulation.py`: fallos de transporte/contrato, límites, fracciones, costes, controles, abstención y ausencia de resultados futuros en la entrada del modelo.

La rama aislada `codex/jev-simulation-20261006` parte del commit local `d78a1c6` con las correcciones de captura y supervisión. No representa un despliegue de ese commit ni de Jev.

## Verificación ejecutada

- Suite completa: **2505 pruebas aprobadas**, 31 advertencias, 62,77 segundos. Incluye 111 pruebas nuevas de integración aislada y mecánica de Jev; las demás pertenecen al bot existente. Entorno limpio, DB temporal y conexiones restringidas a loopback; las respuestas HTTP de Jev son falsas y están declaradas como tales.
- Ruff sobre `src/`, `tests/` y los dos scripts nuevos, formato de los cuatro archivos Python y `git diff --check`: aprobados.
- CLI offline: **15/15 escenarios completados**, cuatro textos únicos, cero consultas a Jev y cero órdenes reales. Casos adversos producen pérdidas ficticias incluso con respuesta favorable; no se omitieron para mejorar el resultado.
- CLI API sin clave: salida explícita `BLOCKED`, código 2, `api_calls: 0`. No se atribuye ninguna evaluación al proveedor.

La suite conserva advertencias de dependencias/deprecaciones y una coroutine de alerta no esperada en un test de OrderbookManagerV2. No se presentan como fallos nuevos de Jev; el log completo está conservado. Este resultado acredita lo probado localmente, no la ausencia de errores en rutas que la suite no cubre.

Evidencia en `/Users/stark/Downloads/botkalshi-P0-cierre/`: `PRUEBAS_JEV_2026-10-06.log`, `JEV_SIMULACION_OFFLINE_2026-10-06.json`, `JEV_API_ESTADO_2026-10-06.json` y `verificar_jev_2026_10_06.py`.

## Alcance exacto de la simulación

Son quince escenarios mecánicos y **cuatro entradas semánticas distintas**; doce escenarios comparten el mismo texto favorable. No son quince observaciones independientes de precisión del modelo.

Los escenarios incluyen alza, reversión tras señal, mercado plano, fill 0,25, liquidez cero, spread amplio, libro vencido, pausa, reinicio sin conciliación, efectivo insuficiente, precio adverso por demora, coste de funding, noticia contraria, instrucción maliciosa y falta de evidencia.

El modo offline usa respuestas prefijadas para verificar el funcionamiento y lo declara como `FIXED_TEST_RESPONSES_NOT_JEV`. No calcula una tasa de acierto de Jev. El modo API registra probabilidades, repetición, modelo, tokens, tiempo y hash, y compara reglas solas con reglas más evaluador.

Supuestos del experimento: entrada larga de hasta una unidad ficticia, sin apalancamiento; precio de compra ask y salida bid; 12 bps taker por cada pata sobre su nocional; funding explícito cuando el escenario lo incluye. Las fracciones se conservan con Decimal. Los umbrales de apoyo 0,90 y variación máxima 0,02 son decisiones de diseño provisionales. No están calibrados con datos financieros. Todos aparecen en el JSON.

El ejercicio no reproduce mínimos, ticks, margen, liquidaciones, cola maker, cancelaciones de Kalshi ni portfolio compartido. Los precios inventados no constituyen un backtest. Una salida futura ausente deja el resultado incompleto; no borra retrospectivamente la entrada ni inventa PnL cero.

## Matriz de entornos y evidencia

| Entorno | Resultado actual | Qué falta |
|---|---|---|
| Local offline | Simulación sintética ejecutada; pruebas del adaptador con transporte falso | No mide comportamiento real del proveedor |
| API Jev | Bloqueada antes de enviar: falta `TYPESAFE_API_KEY` | Cuenta y clave; acceso a la consola |
| Histórico fuera de muestra | Sin evaluar | Datos fechados, etiquetas y reglas congeladas |
| Shadow con datos públicos | Investigación US500 previa; este módulo no añade captura continua | Sesiones completas, costes/unidades reconciliados |
| Kalshi demo | No ejecutado por este trabajo | Acceso demo y pruebas de ejecución/conciliación correspondientes al producto |
| Producción | Sin cambios por este trabajo; ejecución financiera no activada | Corregir bloqueos y completar evidencia independiente |

No se declara validación de “todos los entornos”. Pasar pruebas locales no acredita demo, producción o rentabilidad.

## Cómo ejecutar

Desde este checkout, usando un Python 3.12 con las dependencias del proyecto:

```bash
python -m scripts.jev_simulation --mode offline > jev-offline.json
```

Para el modo API, el titular debe crear su cuenta en la [consola oficial](https://console.typesafe.ai), completar los términos que correspondan y generar su clave. No pegarla en el chat ni guardarla en el repositorio. En una terminal zsh local, después de entrar al checkout:

```zsh
read -rs 'TYPESAFE_API_KEY?Clave de TypeSafe AI: '
export TYPESAFE_API_KEY
python -m scripts.jev_simulation --mode api > jev-api.json
unset TYPESAFE_API_KEY
```

La entrada no se muestra ni incorpora el secreto al historial del comando. Sólo se envían los textos ficticios del script. Si la cuenta exige contratar crédito, eso es una decisión pendiente del titular; no se realizó una compra. `--mode api` sin clave termina con código 2 y `api_calls: 0`; los errores de servicio terminan con código 1 y abstención.

## Pasos posteriores, sin confundir los resultados

1. Ejecutar las treinta consultas iniciales y revisar las cuatro preguntas semánticas únicas y sus repeticiones. Un fallo invalida el lote; no reemplazar silenciosamente la IA con un stub.
2. Si funciona la integración, ampliar por separado a repetición exacta y paráfrasis equivalentes. Luego etiquetar cien textos públicos fechados, reservando setenta posteriores e intactos para evaluación. Medir falsos positivos, abstención y Brier sobre etiquetas semánticas; eso tampoco es calibración de beneficios financieros.
3. Comparar reglas con/sin filtro en las sesiones prospectivas del experimento US500 ya documentado. Mantener comisiones, demora y datos iguales; conservar resultados negativos e inconclusos.
4. Resolver los fallos ya reproducidos: M5 conserva ciertas quotes al vencer cash; cobertura incompleta de exposición/MTM; fills fraccionarios redondeados en motores legacy. Este módulo no modifica esas rutas. Las reparaciones de captura/supervisión previas siguen siendo locales.
5. Validar demo y conciliación del producto antes de cualquier decisión del titular sobre operación real. Añadir Jev no elimina esos requisitos ni demuestra una ventaja económica.
