# Captura y supervisión: corrección del 6 de octubre de 2026

Base: `758351a256b3eb62e9254a7f27165f5cd10987f3`, candidato de captura desplegado el 5/10.
Rama aislada: `codex/capture-supervision-20261006`. No se modifican flags, umbrales de
trading, permisos, órdenes ni datos operativos.

## Problemas y cambios

Con un universo estable, el muestreo REST repetía los primeros 50 elementos de un
set. Ahora recorre los tickers ordenados con un cursor por nombre, manteniendo el
máximo de 50 peticiones por ciclo. Un error de un mercado no monopoliza los ciclos;
un ciclo omitido por presión de disco no consume turno. Para 216 mercados hacen
falta hasta cinco ciclos: esto mejora cobertura, no ofrece datos cada cinco minutos
de cada mercado ni prueba la calidad suficiente para simular fills.

Las fallas terminales de M1, M3, M2 salidas y settlement antes podían registrarse y
dejar terminar la tarea normalmente, sin despertar `FIRST_EXCEPTION`. Ahora se
propagan. Las tareas de captura, settlement y motores habilitados también detectan
retornos inesperados, mientras que el apagado solicitado sigue siendo normal.
Los motores apagados no se construyen ni arrancan.

Ante una falla terminal, el proceso pausa nuevos ciclos y cancela/espera tareas antes
de salir con error. Captura limpia sus subloops incluso si el runner la cancela, y
respeta un apagado que llegó durante su espera inicial, antes de construirse.
Esto no sustituye la conciliación de órdenes ni la política de incidentes durables.

## Verificación

Las pruebas nuevas cubren 120 mercados en tres ciclos, universos pequeños, altas y
bajas de discovery, fallas de peticiones, presión de disco y error al abrir cliente.
Para supervisión cubren propagación terminal, retorno inesperado, limpieza de
hermanos, gates M1/M2 activados/desactivados, shutdown durante arranque y cancelación
de todos los subloops de captura. Se reemplazó una aserción sobre texto fuente por
comprobaciones de comportamiento del runner.

La revisión independiente del diff no encontró regresiones adicionales tras las
correcciones de apagado. Las pruebas usan entorno sin credenciales, bases temporales
y conexiones TCP limitadas a loopback para los servidores WebSocket de prueba.
Resultado: **2.394 pruebas aprobadas en 65,53 segundos**, 31 advertencias; Ruff y
comprobación de formato/diff aprobados. Persisten avisos de deprecación y una coroutine
no esperada en un test legacy V2; no se modificaron en este cambio. Los logs están
en el paquete de entrega del 6/10. La primera pasada detectó dos tests que necesitaban
loopback y una aserción antigua sobre texto fuente, corregidos antes de la pasada final.

## Pendiente antes de evaluar ejecución real

- M5: retirar órdenes resting al perder confirmación fresca de capital.
- Riesgo: valoración de SELL, fills parciales y ausencia de precios frescos.
- M1/M2/M3/REST: cantidades fraccionarias exactas de extremo a extremo.
- M5: fuente de cuotas reales y evaluación neta de costes en una serie.
- US500: módulo propio de datos, margen, financiación, comisiones y contabilidad.
- Cuenta: permisos del producto, saldo y cartera completos y conciliados.

No se desplegó este candidato ni se habilitó ejecución. No mezclarlo automáticamente
en main: el repositorio documenta despliegue automático al hacer merge.
