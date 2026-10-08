# Jev como revisor semántico de mercados

`scripts/jev_market_review.py` revisa una correspondencia propuesta entre un resultado
Kalshi y un resultado Odds usando extractos públicos suministrados. No descubre ni
descarga fuentes, no calcula precios/probabilidades deportivas, no elige operaciones,
no autoriza montos y no se importa desde el runner o los ejecutores.

El perfil es una herramienta manual de asesoría. Incluso `CONSISTENT` conserva
`orders_authorized=false`, `amounts_authorized=false` y `production_ready=false`.
Tampoco resuelve incidentes de órdenes DEMO ni cambia sus journals.

## Contrato de entrada

JSON con `schema="jev-market-pairs-v1"` y de 1 a 12 elementos en `pairs`. Cada par
tiene exactamente `id`, `evidence_kind`, `kalshi` y `odds`. `evidence_kind` puede ser
`public_snapshot` o `synthetic_fixture`; no se envía al modelo, igual que el ID local.
Un contrafactual o una entrada adversaria deben etiquetarse `synthetic_fixture`.

Cada proveedor lleva las diez claves de este ejemplo. Son metadatos semánticos;
campos adicionales como `price`, cuenta, stake o credenciales se rechazan.

```json
{
  "schema": "jev-market-pairs-v1",
  "pairs": [
    {
      "id": "fixture-correspondencia-01",
      "evidence_kind": "synthetic_fixture",
      "kalshi": {
        "event_id": "fixture-kalshi-01",
        "title": "Team Alpha vs Team Beta",
        "sport": "baseball",
        "participants": ["Team Alpha", "Team Beta"],
        "scheduled_start": "2026-10-08T23:00:00Z",
        "outcome": "Team Alpha wins",
        "rules": "Final winner including extra innings. Canceled games void. Ties void.",
        "source_url": "https://external-api.kalshi.com/trade-api/v2/markets/fixture-kalshi-01",
        "retrieved_at": "2026-10-08T16:00:00Z",
        "source_text": "fixture-kalshi-01\nTeam Alpha vs Team Beta\nbaseball\n2026-10-08T23:00:00Z\nTeam Alpha wins\nFinal winner including extra innings. Canceled games void. Ties void."
      },
      "odds": {
        "event_id": "fixture-odds-01",
        "title": "Team Alpha vs Team Beta",
        "sport": "baseball",
        "participants": ["Team Alpha", "Team Beta"],
        "scheduled_start": "2026-10-08T23:00:00Z",
        "outcome": "Team Alpha wins",
        "rules": "Final winner including extra innings. Canceled games void. Ties void.",
        "source_url": "https://api.the-odds-api.com/v4/sports/baseball/events/fixture-odds-01",
        "retrieved_at": "2026-10-08T16:00:00Z",
        "source_text": "fixture-odds-01\nTeam Alpha vs Team Beta\nbaseball\n2026-10-08T23:00:00Z\nTeam Alpha wins\nFinal winner including extra innings. Canceled games void. Ties void."
      }
    }
  ]
}
```

Este ejemplo es ficticio, no un snapshot ni un resultado medido de Jev. Sus horas
de obtención quedarán obsoletas; no cambiarlas para presentar un snapshot antiguo
como recién capturado. Para datos reales, suministrar extractos auténticos y su
hora de obtención real. No escribir reglas supuestas: si Odds no aporta el criterio
de liquidación, usar `rules: null`; el resultado será `REVIEW` sin consulta API.

`scheduled_start` debe ser el inicio del evento con zona horaria explícita, nunca
`close_time` o liquidación. No asumir identidad por similitud del ticker o de los
nombres. `outcome` identifica qué resultado se está comparando, no una cotización.

Los campos semánticos y `event_id` deben aparecer literalmente en `source_text`.
Esta comprobación sólo acredita inclusión textual; no autentica la fuente ni
prueba la relación contextual. El modelo evalúa el contexto después del filtro.
La salida mantiene `source_authenticity_verified=false`. Los enlaces no se visitan.
Se aceptan únicamente hosts oficiales declarados en `HOSTS`, HTTPS, sin credenciales,
query ni fragmento. Nunca incluir URLs Odds con `apiKey`. Los extractos deben estar
previamente reducidos a información pública pertinente, sin precios ni datos privados.

Falta de campos, citas no ancladas, indicadores de instrucciones adversarias y fuentes
futuras u obsoletas producen `REVIEW` local y cero llamadas. El detector de instrucciones
es heurístico: no garantiza detectar todas las inyecciones. La ausencia de acceso a
acciones limita el efecto de un posible error semántico.

## Preparar y ejecutar un lote acotado

Desde el checkout candidato, con su Python del entorno virtual:

```sh
python -m scripts.jev_market_review --input /ruta/pares-publicos.json --max-calls 3
```

Éste es el modo por defecto. No lee claves, crea journals ni solicita inferencias.
El informe devuelve el `packet_sha256` que congela entradas, preguntas, modelo,
versión, antigüedad permitida y presupuesto. Revisar los datos suministrados y el
informe antes de utilizar ese SHA.

Sólo una ejecución explícita `--mode api` puede usar TypeSafe:

```sh
python -m scripts.jev_market_review \
  --input /ruta/pares-publicos.json --max-calls 3 \
  --mode api --expected-sha256 SHA_DEL_PREPARE \
  --journal /ruta/privada/jev-market-review.sqlite \
  --key-file /ruta/privada/typesafe-api-key
```

El archivo de clave debe ser regular, privado del usuario y sin symlink. No se
leen `.env`, claves Kalshi, claves Odds ni variables de proveedor. El cliente fija
`https://api.typesafe.ai/v1/systemone`, deshabilita proxies heredados/redirecciones
y conserva el modelo `jev-1.13.0`. El adaptador batch reutiliza el transporte,
lock, cotas y parser Noul estricto de `scripts/jev_client.py`; ese cliente no cambia.

No borrar ni sustituir el journal para repetir un fallo. Reutilizar la misma ruta
conserva resultados y presupuesto. El máximo es 12 llamadas por journal; un intento
fallido o interrumpido también consume una. El límite no aumenta al reabrir y no
hay reset automático. Crear otro journal es otro experimento, no una recuperación.

La reserva se confirma en SQLite con `BEGIN IMMEDIATE` y `synchronous=FULL` antes
de HTTP. Dos procesos comparten el presupuesto. Una caída entre reserva y resultado
deja `RESERVED`: no se vuelve a enviar ese estado, aunque quizá no alcanzó al proveedor.
Un error deja `FAILED` y detiene el resto de ese lote; otro recorrido puede continuar
otros estados dentro del presupuesto, sin reintentar el fallido. Se conserva cada
resultado confirmado antes de pasar al siguiente.

El caché se identifica por el SHA del cuerpo exacto (estado, ambas preguntas y modelo).
Cambiar reglas, equipos, fecha, fuente, hora de obtención o prompt invalida la identidad.
La frescura se comprueba nuevamente antes de consumir un resultado cacheado. Los
duplicados exactos reutilizan una inferencia y no aumentan el tamaño de muestra.
Un caché inválido produce `REVIEW`, nunca una llamada de reemplazo automática.

## Significado del resultado

Cada solicitud contiene dos Nouls independientes, sin compartir respuestas:

1. `conflict`: probabilidad de que los extractos acrediten una incompatibilidad concreta.
2. `correspondence`: probabilidad de que respalden el mismo evento/resultado y condiciones.

| Resultado | Regla del código |
| --- | --- |
| `CONSISTENT` | Entradas completas, frescas y ancladas; `conflict <= 0.1` y `correspondence >= 0.9`. |
| `INCONSISTENT` | Entradas completas, frescas y ancladas; `conflict >= 0.9`, `correspondence <= 0.1` y al menos un campo semántico distinto. |
| `REVIEW` | Cualquier otro caso, incluidas dos respuestas altas, dos bajas, fallo API, ausencia o incertidumbre. |

`differing_fields` muestra diferencias textuales observables; no es una explicación
generada ni atribuye al modelo una causa concreta. Las fechas se comparan como
instantes y el orden de los dos participantes no crea una diferencia por sí solo.
No-conflicto no equivale a correspondencia. Estos umbrales son provisionales y no
están calibrados con un conjunto independiente de mercados reales.

Máximos: 12 pares, 256 KiB de entrada, 5 KiB por extracto, 16 KiB por solicitud,
64 KiB por respuesta, antigüedad 1–86400 s (3600 s por defecto). El transporte usa
timeout de 10 s por fase y una cota transcurrida de 30 s revisada entre lecturas;
no es un deadline absoluto y una lectura puede añadir hasta su timeout. El informe
separa tokens conocidos de intentos con consumo desconocido; no promete un coste
monetario exacto para llamadas sin respuesta.

## Fuentes y validación

Contrato público consultado el 8 de octubre de 2026:

- [API](https://docs.typesafe.ai/api): petición `model/state/questions`, respuestas tipadas y usage.
- [Noul](https://docs.typesafe.ai/primitives/noul): probabilidad de sí; incertidumbre no es intensidad; preguntas independientes juntas.
- [Citation check](https://docs.typesafe.ai/cookbooks/citation_check): comprobar primero la cita literal y luego su contexto; caché y revisión humana.
- [Limitaciones Jev 1.13](https://docs.typesafe.ai/model-jaggedness/jev-1.13): el resultado tipado no garantiza verdad ni resistencia completa a instrucciones maliciosas.

Pruebas offline en `tests/scripts/test_jev_market_review.py`: batch, contrato inválido,
límites, incertidumbre, entrada adversaria, pérdida de evidencia, fuente obsoleta,
caché y cambio de estado, reserva concurrente, caída abrupta y recuperación sin retry,
separación de preparación/API, ausencia de importaciones del bot y de secretos en salida.
No son una medición de exactitud de Jev ni acreditan una integración operativa.
