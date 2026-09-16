# 02 — Observador de mercados y control de omisiones

## Alcance del informe general

Verificar el día solicitado en America/Los_Angeles. Hoy/mañana mantiene el alcance completo salvo restricción expresa; en ese caso rotular INFORME PARCIAL. Un catálogo de contratos no sustituye el calendario oficial ni una lista de partidos equivale a recomendación.

| Categoría obligatoria | Verificación mínima |
|---|---|
| MLB | Calendario oficial, abridor/opener confirmado o previsto, límite de entradas, bullpen/carga reciente, alineación y estadio. |
| NFL | Calendario, disponibilidad, hándicap exacto y condiciones pertinentes. |
| Fútbol americano universitario | Universo de competiciones declarado y calendario oficial; no llamar sin eventos a toda NCAA por haber revisado sólo FBS. |
| UEFA Champions League | Jornada y horarios UEFA, contraste de bajas/once en clubes; 90 minutos, empate y clasificación diferenciados. |
| Liga MX | Calendario oficial, noticias de equipos, descanso, sanciones, localía y once. |
| Leagues Cup | Calendario propio; no confundir con Campeones Cup u otra competición. |
| Acciones | Bolsa/sesión, instrumento exacto, liquidez/spread, horizonte, resultados y comunicados del emisor. |
| Oro | Instrumento y mercado reales, referencia fechada, datos macro y bancos centrales. |
| Criptomonedas | Exchange/par exactos, precio/velas/volumen verificables, costes; funding/OI sólo donde corresponda. |
| Divisas | Par/instrumento/sesión, calendario oficial macro, costes y acceso. |
| Contratos financieros Kalshi | Reglas, resolución, vencimiento, payout, libro y tarifas del producto. |

El informe conserva una fila por categoría: EVENTOS_VERIFICADOS, SIN_EVENTOS, ANALIZADA_SIN_ENTRADA, PENDIENTE_DATOS o NO_REVISADA, con fuente y motivo. Son estados de cobertura, no recomendaciones. No asignar SIN_EVENTOS sin calendario verificado, ni ANALIZADA_SIN_ENTRADA a algo no analizado. Si se corrige una omisión, revisar también la vigencia de los precios citados antes; no agregar automáticamente una apuesta.

## Descubrimiento y límites

Separar inventario del proveedor, matching con eventos y captura de libros. Mapear series a categorías sólo con especificaciones verificadas; no inventar tickers ni decir que KXMLBGAME cubre otras ligas. Recorrer cursores por categoría dentro de un presupuesto global de llamadas/tiempo/almacenamiento explícito. Registrar páginas, repetición de cursores incluso A-B-A, duplicados, filas rechazadas, truncación y ventana temporal. Un error de proveedor no equivale a conjunto vacío.

Conservar límites existentes y medir recursos antes de ampliarlos. El wrapper del PR #255 permite más orderbooks que el collector base; no convertir sus defaults en autorización de más consumo. Una categoría ausente queda pendiente/no revisada, no ocupa silenciosamente el presupuesto de otra ni desaparece del informe.

`close_time`, `expiration_time` y comienzo deportivo son campos distintos. Verificar comienzo en la fuente oficial; sin él no certificar PREMATCH. No trasladar umbrales prepartido a eventos en vivo. La ventana se calcula por zona horaria IANA, no por una fecha UTC heredada.

## Cotización utilizable

Cada observación lleva proveedor, instrumento/ticker, event_id, fase, hora del proveedor cuando existe, hora local de recepción, edad y hash de reglas. Preservar timestamps originales de datos cacheados. Un timestamp de fin de ciclo no renueva el dato.

Guardar bids/asks o derivación explícita y correcta del libro YES/NO, orden de niveles, cantidades y unidad de precio. Kalshi documenta bids de ambos lados; verificar el formato vigente antes de derivar un ask del lado opuesto. No usar la última operación ni un porcentaje visible como ask ejecutable. Validar precios, cantidades decimales, profundidad, libros vacíos/cruzados y subpenny; no truncar precisión. [S10](07-FUENTES.md).

Conservar comisiones oficiales específicas y redondeo por orden; no asumir una tarifa global o descuentos de cuenta. Si falta fee/payout/regla/precio/tamaño mínimo, la propuesta queda ESPERAR. Comprar un contrato sobre oro, acciones o FX no es poseer esos activos.

## Paquetes coherentes

Cobertura, health y observaciones deben enlazarse por cycle_id, versión, hashes y tiempos. Publicar un manifest sólo después de escritura duradera de todos los artefactos; rechazar ciclos incompletos, datos viejos/futuros o errores. Mantener separados capturado, fresco, calendario completo, analizado y contrato operable: un health verde no acredita los demás.

Investigar candidatas de todas las categorías con evidencia disponible, buscar evidencia contraria y comparar dos referencias actuales del mismo mercado cuando sea posible, sin promediar modelos correlacionados a ciegas. Ordenar como máximo tres candidatas TOTALES, nunca tres por categoría. No inventar RSI, probabilidades, backtests, liquidez o EV para llenar espacios.
