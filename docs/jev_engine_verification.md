# Jev como segunda revisión de evidencia de motores

Este experimento mide si Jev distingue una violación concreta de su control a partir
de trazas ficticias relacionadas con el código del bot. Las pruebas deterministas y
las reproducciones ejecutan el código por separado. Jev no ejecuta tests, descubre
archivos ni certifica la implementación. No hay integración con `src/`, órdenes,
configuración de trading o base de datos.

## Diseño congelado antes de consultar al proveedor

- Modelo fijo `jev-1.13.0`, cliente acotado ya probado y endpoint oficial.
- Veinticuatro casos: doce con defecto demostrado y doce sin evidencia del defecto
  específico. Los casos con evidencia insuficiente pertenecen a la segunda categoría;
  no se interpretan como motores correctos.
- Dos consultas idénticas por caso; máximo 48 sin reintentos. Se detiene el lote al
  primer error de transporte, HTTP o contrato. La salida conserva el avance parcial.
- La API recibe exclusivamente `criterion` y `evidence`. ID, motor, etiqueta esperada,
  procedencia y clasificación como reproducción/contrafactual quedan locales.
- Un solo juicio por estado: si la evidencia establece la violación especificada.
  Las peticiones separadas corresponden a estados distintos y a repeticiones exactas;
  no se fragmenta un mismo estado en preguntas que podrían agruparse.
- `FLAG`: ambas respuestas >=0,90; `NO_DEFECT_SHOWN`: ambas <=0,10. Cualquier otro
  resultado o variación mayor a 0,02 produce `REVIEW`. Estos umbrales son provisionales.
- Matriz de errores y Brier: primera respuesta de cada caso, umbral diagnóstico 0,50.
  La repetición mide estabilidad y no duplica artificialmente la muestra de exactitud.
  El JSON también conserva probabilidades y clasificación de revisión.
- La clave se lee de un archivo regular privado del usuario, fuera del repositorio.
  No se acepta por argumento, no se imprime y no se incluye en la salida.
- Salida creada de forma exclusiva: un archivo existente impide repetir el lote.
  El hash de los casos incluye etiquetas y procedencia; el del prompt queda registrado.
- Checkpoints en temporal y reemplazo atómico conservan el resultado anterior ante un
  fallo de escritura. Un intento sin uso confirmado queda marcado como coste desconocido;
  la estimación suma únicamente el uso informado en respuestas válidas.

Se reutiliza el cliente con el ID HTTP histórico `supports_trade`. El proveedor
documenta que los identificadores no llegan al modelo; aquí la instrucción pregunta
por un defecto y el informe lo llama `probabilities_defect`. Ninguna respuesta se
consume como una decisión de trading. [Contrato API](https://docs.typesafe.ai/api).

## Uso

Desde el checkout con Python 3.12 y dependencias de desarrollo:

```bash
python -m scripts.jev_engine_verification
```

La preparación valida casos y muestra su hash y presupuesto, sin leer la clave ni
llamar a la API. Para un lote autorizado con evidencia ficticia revisada:

```bash
python -m scripts.jev_engine_verification --mode api --output /ruta/nueva/resultado.json
```

La ruta predeterminada de la clave es `~/.config/botkalshi/typesafe-api-key`.
`--key-file` permite señalar otro archivo privado; nunca colocar la clave literal
en ese argumento. No se cargan `.env` ni credenciales del bot. La tarifa usada para
estimar el consumo es US$0,042 por millón de tokens de entrada, salida gratuita,
consultada el 6 de octubre de 2026. [Modelo y tarifa](https://docs.typesafe.ai/models).

El patrón es una comprobación de afirmaciones contra evidencia, inspirado en el
[ejemplo oficial de verificación](https://docs.typesafe.ai/cookbooks/citation_check).
Se usa `noul` para el único juicio binario y se conservan valores crudos; una
probabilidad tipada no demuestra corrección. [Noul](https://docs.typesafe.ai/primitives/noul).

## Interpretación y límites

Los casos fueron diseñados por revisores, no muestreados de producción ni reservados
como evaluación independiente. Parte representa defectos presentes y parte mutaciones
hipotéticas: la procedencia permite distinguirlas. Un resultado alto no acredita
recall general, tolerancia universal a inyección ni calibración en incidentes reales.

`NO_DEFECT_SHOWN` nunca significa "motor seguro". Los escenarios no cubren todo el
ciclo de vida, ni demuestran ejecución demo, fondos, elegibilidad, retorno o producción.
El siguiente uso razonable es revisión auxiliar de incidentes con evidencia mínima y
una comparación local verificable; los tests y los controles de ejecución siguen en código.

## Resultado del 6 de octubre de 2026

La ejecución con API completó los 24 casos y 48 respuestas sin errores. El primer
intento coincidió con la etiqueta local en 23 casos; el segundo, en 24. Con el criterio
conservador predefinido hubo 2 `FLAG`, 6 `NO_DEFECT_SHOWN` y 16 `REVIEW`.

El caso M2 de fill confirmado de 0,25 perdido por redondeo produjo **0,47 y 0,51**:
un defecto reproducido quedó como falso negativo en la primera clasificación a 0,50
y cambió de lado en la repetición idéntica. No se ajustó el prompt ni el umbral después
para ocultarlo. La mayor variación entre repeticiones fue 0,11 en el control MTM.

De los tres fallos actuales representados, sólo las quotes M5 con saldo vencido
alcanzaron `FLAG` (0,93/0,94). Fracción M2 y MTM parcial M5 quedaron en `REVIEW`.
Un segundo `FLAG` corresponde al contrafactual M8 que inventa mids durante un book caído;
no es un defecto observado del shadow actual.

Uso: 25.328 tokens de entrada, coste estimado de modelo US$0,001063776 y latencia
mediana 106,81 ms en este lote. El conjunto final tiene SHA-256
`ce5a8de7f6edcf0236c2d21382dc7db1002f55353950eb1ff1dfcdc1da4e8d9e`.
El criterio de M3 se aclaró **antes** del API para juzgar cantidad enviada, sin
atribuir un fill a una IOC sólo por haberla enviado.

Evidencia en el paquete local: `JEV_MOTORES_API_2026-10-06.json`,
`JEV_MOTORES_PREPARACION_2026-10-06.json`, `JEV_MOTORES_ORACULO_2026-10-06.md`
y `JEV_ORACULO_RESULTADOS_2026-10-06.json`.

**Decisión de diseño:** mantener este evaluador como segunda revisión manual, fuera
del runtime financiero. En esta muestra no reemplaza tests, reproducción o revisión
del riesgo. No se han reparado aquí las rutas de los motores.
