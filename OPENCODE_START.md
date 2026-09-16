# OpenCode — inicio del trabajo Kalshi

Fecha: 2026-09-16. Entrega documental para continuar el proyecto existente. No es un despliegue, una sesión de OpenCode iniciada ni una autorización financiera.

## Encargo

Construye sobre `Noahstark23/botkalshi` un observador verificable de mercados, un lector separado de cuenta y un conciliador que produzcan evidencia utilizable por el asistente. El asistente analiza, propone y comprueba límites; no administra discrecionalmente dinero ni envía órdenes. No reescribas el bot ni reactives motores antiguos para avanzar más rápido.

Lee primero las instrucciones locales aplicables que ya existan, sin sobrescribirlas. Después abre explícitamente los documentos de esta entrega; mencionarlos no equivale a haberlos leído:

| Orden | Documento | Resultado esperado |
|---|---|---|
| 1 | [00-ESTADO.md](docs/opencode/00-ESTADO.md) | Distinguir código, evidencia, defectos y bloqueo remoto. |
| 2 | [01-SEGURIDAD.md](docs/opencode/01-SEGURIDAD.md) | Aislamiento técnico y cero facultad de órdenes. |
| 3 | [04-RIESGO.md](docs/opencode/04-RIESGO.md) | Corregir el guard sin inventar capital ni conciliación. |
| 4 | [06-PRUEBAS-Y-ENTREGA.md](docs/opencode/06-PRUEBAS-Y-ENTREGA.md) | Ejecutar la primera tarea y entregar pruebas reproducibles. |
| 5 | [02-OBSERVADOR.md](docs/opencode/02-OBSERVADOR.md) | Cobertura y precios con identidad y tiempo. |
| 6 | [03-CUENTA-Y-CONCILIACION.md](docs/opencode/03-CUENTA-Y-CONCILIACION.md) | Lectura privada y contabilidad verificable. |
| 7 | [05-PUENTE-IA.md](docs/opencode/05-PUENTE-IA.md) | Paquete de evidencia y contrato del informe. |
| 8 | [07-FUENTES.md](docs/opencode/07-FUENTES.md) | Contrastar documentación oficial y versiones. |

## Primera tarea acotada: P0, no todo el roadmap de una vez

Inspecciona el checkout, su SHA y cambios locales, y confirma versión de Python y de OpenCode. No instales ni actualices herramientas para averiguar la versión. Lee el guard y sus tests. En una rama/copia aislada, reproduce el defecto de aceptación de fecha/fuente arbitrarias y entrega una corrección con pruebas para fecha inválida, futura, vieja y datos incompletos. No presentes una cadena `source` ni un hash como prueba de conciliación real. Hasta existir la validación de cuenta completa, cualquier cálculo permanece ilustrativo y `real_entry_eligible=false`.

Comprueba también que importar los módulos no inicia red ni jobs. Mantén `execution_authorized=false` y `order_capability_present=false`. Publicar documentación o aprobar tests no cambia esos permisos. Entrega diff, entorno, comandos, resultados exactos y pendientes; no afirmes que la suite pasó sin ejecutarla.

## Restricciones de esta entrega

No hacer órdenes, cancelaciones, depósitos, retiros, transferencias, préstamos, cambios de cuenta, rotación de claves, cambios en SSH/firewall ni reinicios de servicios. No fusionar PRs, cambiar `main`, modificar workflows de despliegue, contratar proveedores o aumentar recursos. No usar `src.runner`, el compose raíz ni ejecutores heredados. No pedir credenciales por chat ni leer archivos secretos hacia el contexto del modelo.

No saltarse denegaciones de acceso o extracción. El candidato privado `prdry.tgz` sigue pendiente: este paquete no lo contiene ni lo sustituye. Conservar cambios locales y no reinstalar por defecto.

## Cómo cargar estas instrucciones

Esta entrega no modifica `AGENTS.md`, `CLAUDE.md`, `opencode.json` ni agentes instalados. El operador abre su copia autorizada del proyecto en OpenCode y envía:

> Lee OPENCODE_START.md y sus documentos en el orden indicado. Empieza únicamente por P0: inspección y corrección offline de frescura/validación del risk guard con regresiones. Preserva el trabajo existente y el pendiente prdry.tgz. No despliegues, uses credenciales, actives trading ni contrates servicios. Devuelve evidencia de lo hecho y un siguiente paso concreto.

Consulta los permisos de la versión realmente instalada antes de configurar agentes: OpenCode V1 y V2 no comparten toda la sintaxis. No arrancar en modo de autoaprobación. Tener estos MD en GitHub no demuestra que una instancia de OpenCode los haya cargado.
