# 11. Encargo ejecutable de trabajo para OpenCode/Codex/Claude Code

Estas instrucciones son para una sesión de desarrollo supervisada. No crean un agente en segundo plano ni dan acceso a proveedores. No reemplazar instrucciones raíz existentes sin revisar conflictos; en seguridad prevalecen los límites explícitos del usuario y este alcance sin órdenes.

> Trabaja en `Noahstark23/botkalshi`, rama separada de main. Lee `docs/relaunch/README.md` y los documentos enlazados. Consulta primero HEAD y conserva cambios locales. La base examinada es bcc365f20a04b7ef3123da18983879b006290bb9. Si main avanzó, compara los módulos afectados antes de aplicar instrucciones históricas.
>
> Ejecuta únicamente R1 inicialmente: inventario y evaluación de snapshot existente de solo lectura. Si falta DB o autorización, usa una fixture sintética, no una base vacía etiquetada producción. No ejecutes src.runner, compose raíz, smoke de órdenes o script cuyo comportamiento no hayas inspeccionado.
>
> Reutiliza M2 como fair y M5 como laboratorio prepartido; no reabras motores archivados. No cambies criterios F1 ni parámetros de cohorte para mejorar resultados. La IA no reemplaza el policy engine ni calcula probabilidades como hechos sin fuente.
>
> Antes de R2/R3, identifica el transporte común y demuestra que todo intento de escribir a Kalshi falla sin salir por red. Importar un módulo no debe arrancar jobs. No uses una API genérica con firma accesible desde la IA. Configura test isolation sin variables reales, red externa bloqueada y loopback limitado para tests locales.
>
> Cada cambio debe tener prueba que falle en la versión defectuosa y pase tras la corrección. Los tests históricos K01–K08 reproducen bugs, no certifican un fix. Respeta Decimals, paginación, fill/orden separados, UNKNOWN y reservas atómicas. Valida endpoints/respuestas con documentación externa fechada.
>
> No uses claves del historial, no imprimas secretos, no despliegues ni contrates servicios. No modifiques workflows de despliegue, cuenta financiera, licencia, protecciones de rama, fondos o flags de ejecución. No fusiones PRs automáticamente. No uses este chat como API de servidor ni intentes convertir la suscripción en consumo programático no autorizado.
>
> Entrega: archivos cambiados, comandos de test y resultados exactos, límites de cobertura, riesgos pendientes, costo adicional solicitado (cero por defecto) y una sola siguiente tarea concreta. No afirmar producción lista ni rentabilidad por compilar.

## Definiciones de funciones propuestas

`export_research_packet` devuelve evidencia saneada, nunca claves ni acceso DB. `evaluate_proposal` solo calcula una propuesta simulada con estado explícito. `record_assessment` persiste evaluación relacionada con packet_hash, no una orden. `read_research_report` lee por identificador acotado, no por path/SQL/URL libre. Implementar contratos en PRs pequeños, sin sustituir de golpe la arquitectura existente.

## Responsabilidad

Asistente de programación: preparar/revisar código y tests en la sesión. Operador: ejecutar en su entorno autorizado, verificar secretos y aprobar gastos o cambios operativos. Revisor: contrastar dinero/seguridad con evidencia independiente. Una revisión programada no es monitoreo intradía ni garantía de cancelación de órdenes.
