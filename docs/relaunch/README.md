# botkalshi: relanzamiento de investigación e ingeniería

Fecha: 2026-09-14. Repositorio: `Noahstark23/botkalshi`.
Base consultada: `bcc365f20a04b7ef3123da18983879b006290bb9`.
Estado de esta entrega: **diseño y especificaciones; no despliegue ni versión reparada**.

## Decisión

Reutilizar el backend Python existente. Recuperar primero su laboratorio M5 prepartido y conectar sus observaciones con una capa IA de análisis, nunca con una autorización de órdenes. El resultado mínimo útil es un informe reproducible, con procedencia, precio/edad, costos, cobertura y motivos de abstención, aunque no haya una operación.

El valor profesional se mide por corrección, pruebas, seguridad, reproducibilidad y capacidad de explicar decisiones. La rentabilidad es una hipótesis independiente. El objetivo ilustrativo de USD 40 mensuales no es una promesa, un criterio para relajar riesgos ni una partida para pagar gastos esenciales.

## Orden de lectura

| Documento | Entrega |
|---|---|
| [01-ESTADO-Y-NIVEL.md](01-ESTADO-Y-NIVEL.md) | Evidencia actual, brechas y valoración profesional |
| [02-ARQUITECTURA.md](02-ARQUITECTURA.md) | Componentes existentes, propuestos y límites de autoridad |
| [03-INFRAESTRUCTURA.md](03-INFRAESTRUCTURA.md) | Perfiles, preparación local, despliegue y reversión |
| [04-SEGURIDAD.md](04-SEGURIDAD.md) | Modelo de amenazas y controles verificables |
| [05-RIESGO-Y-CONTABILIDAD.md](05-RIESGO-Y-CONTABILIDAD.md) | Capital experimental, reservas y P&L |
| [06-DATOS-E-IA.md](06-DATOS-E-IA.md) | Contratos, conexión con ChatGPT/MCP/API y abstención |
| [07-EXPERIMENTOS-Y-PRUEBAS.md](07-EXPERIMENTOS-Y-PRUEBAS.md) | M5, validación fuera de muestra y pruebas adversariales |
| [08-ROADMAP.md](08-ROADMAP.md) | PRs pequeños, dependencias y aceptación |
| [09-OPERACION-Y-COSTOS.md](09-OPERACION-Y-COSTOS.md) | Runbook, observabilidad, presupuesto y continuidad |
| [10-CV-Y-DEMO.md](10-CV-Y-DEMO.md) | Evidencias para entrevistas sin inflar credenciales |
| [11-ENCARGO-AGENTES.md](11-ENCARGO-AGENTES.md) | Instrucciones para OpenCode, Codex o Claude Code |
| [12-FUENTES.md](12-FUENTES.md) | Fuentes y alcance de la comprobación |

Los JSON de [specs/research-v1](../../specs/research-v1/) son contratos propuestos, **no configuración consumida por el runner actual**. La fixture es enteramente sintética. Ninguna contiene cotizaciones, posiciones o capital real del usuario.

## Límites no negociables

No reactivar motores archivados. No usar secretos históricos. No conectar producción con facultades de escritura. No desplegar el compose antiguo como si fuera un perfil seguro. No crear VPS, suscripciones, depósitos ni órdenes por publicar estos archivos. No fusionar automáticamente este trabajo a `main`.

Primera aceptación: inventario reproducible y evidencia de que el proceso de investigación no puede enviar órdenes, incluso cuando un flag está mal configurado. Hasta entonces: fixtures y copias de datos expresamente disponibles, sin iniciar `src.runner`.
