# 08. Roadmap por PR y definición de terminado

No hay despliegue automático ni plazo prometido. Ejecutar una etapa activa a la vez. Cada PR incluye diff acotado, tests, evidencia saneada, limitaciones, rollback y costos nuevos. Los siguientes son trabajos propuestos, no issues ya creados.

| PR | Alcance | Dependencia | Aceptación concreta |
|---|---|---|---|
| R0 | Estos MD, esquema y política de referencia | Ninguna | Archivos revisables, sin cambios en src, runtime, secretos ni capital |
| R1 | Inventario/snapshot read-only y drift | R0 | SHA/config saneada, esquema DB y estado de cohorte; fixture si faltan datos |
| R2 | Perfil de investigación aislado | R1 | Transporte bloquea mutaciones; no importa executor; pruebas de todas las combinaciones de flags |
| R3 | Lock y build reproducible local | R2 | Dependencias únicas; digest; CI; arranque offline sin claves ni red; restore |
| R4 | Exportador y contratos de datos | R3 | Paquete saneado con hash, timestamps, cobertura y máximo tres finalistas; nulos explícitos |
| R5 | Uso interactivo de IA + evaluación separada | R4 | Baseline sin modificar, validación semántica y registro de costo/calidad |
| R6 | Captura M5 acotada y evaluación OOS | G0/G1 y presupuesto | Serie/cohorte congeladas, reglas F1 y escenarios de ejecución/costos |
| R7 | Reparar K01–K08 para mecánica demo | G2 defendible o valor educativo explícito | Regresiones, rutas/respuestas actuales, sin habilitar producción |
| R8 | Demo técnica para CV | R4 y casos reproducibles | Video/demostración con fixtures, informe de incidentes y cifras honestas |

R5 no es dependencia obligatoria de R6: una IA inaccesible no debe impedir evaluar M5 baseline. MCP/API solo se añaden mediante ADR posterior si resuelven una necesidad medida; no bloquear el proyecto construyendo otra plataforma de agentes.

## Primera tarea para el implementador

Inspeccionar repo/estado local; localizar configuración y entradas de runner; recuperar un snapshot autorizado si existe; ejecutar evaluador de lectura sobre copia. Si falta información, entregar fixture y lista exacta de ausencias. No pagar otra cohorte ni reinstalar todos los servicios antes de saber qué datos ya existen.

El bloqueo K07 exige aislamiento verificable ANTES de dar al proceso acceso autenticado, aunque se posponga la reparación completa del executor. Cambiar un booleano no satisface R2. Alternativa segura mientras tanto: offline o endpoints públicos sin credenciales, con transporte de lecturas explícitas.

## Definición global de terminado

Un tercero puede reproducir un informe desde un snapshot/fixture con SHA y política conocidos; el reporte distingue cobertura/pendientes; ningún intento de mutación llega a Kalshi; nulos no se vuelven cero; backups se restauran; tests nuevos fallan frente al defecto; presupuesto y logs no exponen secretos.

No marcar terminado porque haya carpetas, un Dockerfile, un dashboard bonito o un CI verde basado únicamente en importaciones.

## Fuera de alcance inicial

Reactivar M1, M2 direccional, REST, M6, M8 o M9; M4 entre exchanges; perps/margen; modelos entrenados desde cero; auto-ejecución; productos para vender señales; nuevas compras/VPS; cambios de permisos de cuentas. No se cambia la licencia ni se promete redistribución comercial de datos ajenos.
