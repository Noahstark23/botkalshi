# 04. Seguridad y modelo de amenazas

## Activos y límites de confianza

Proteger credenciales, fondos, integridad del ledger, políticas, evidencia experimental y datos personales. Noticias, páginas, libros externos, texto del modelo y archivos importados son entradas no confiables. GitHub público y los informes del CV nunca reciben estados de cuenta, balances personales, tokens, PEM, cookies ni archivos de producción.

| Amenaza | Control requerido | Prueba de aceptación |
|---|---|---|
| Clave histórica expuesta | Revocar/verificar fuera del repo y generar nueva si hace falta | Evidencia sin secreto de revocación; nunca recuperar la clave del historial |
| Escritura accidental con flags off | Transporte de lectura con lista explícita de método+ruta; ningún executor en proceso | Todos los intentos de mutación fallan antes del socket |
| Prompt injection | Modelo sin shell, secretos, SQL arbitrario ni tools de escritura | Documento malicioso no altera política ni llama a órdenes |
| SSRF/exfiltración vía fuentes | Ingestor controlado; URLs validadas, sin destinos privados/metadata ni redirects libres | Casos localhost, IP privada, DNS rebinding y redirects rechazados |
| Libro viejo o incompleto | Secuencia, snapshot, TTL y pausa fail-closed | Gap/reconnect no produce propuesta válida |
| Doble orden por retry | Intención durable/idempotente; estado desconocido conserva reserva | Timeout no duplica orden ni libera riesgo |
| Robo por panel público | Loopback/red privada, autenticación y mínimas rutas | Sin acceso público; ningún endpoint mutante |
| Supply chain | Lock, imagen fijada, análisis de dependencias y secretos, CI sin claves | PR no puede leer credenciales de producción |
| Corrupción/borrado de DB | Backup consistente, verificación y restore ensayado | Recuperación de IDs, cantidades y estados sin pérdidas silenciosas |

## Aislamiento de IA

El modelo ve paquetes saneados de tamaño limitado y evidencia por identificador. No recibe claves, variables de entorno, rutas arbitrarias o capacidad de construir peticiones autenticadas. Una salida conforme al JSON Schema puede ser falsa: se verifican también referencias, orden temporal, rangos y contrato.

Para MCP: ofrecer únicamente herramientas de lectura de informes/paquetes específicos. No exponer `execute_sql`, `run_shell`, `http_request`, `place_order`, `cancel_order`, cambios de límites ni funciones de pagos. El protocolo MCP puede usar POST para lecturas; la prohibición de métodos mutantes se aplica al transporte hacia Kalshi, no a una suposición de que todo POST de cualquier protocolo es una orden.

Un MCP remoto requerirá autenticación, TLS, scopes mínimos, cuotas y protección contra exportación masiva. No publicar un túnel al portátil para saltarse estos controles. Disponibilidad en ChatGPT y permisos de la cuenta deben verificarse antes de conectarlo.

## Secretos y acceso

Archivos de secretos fuera del checkout, permisos restrictivos y montaje solo lectura únicamente en el componente que los necesita. Logs con redacción de cabeceras y cuerpos sensibles; no imprimir entorno completo ni `docker compose config` con secretos expandidos. Usar parámetros no secretos y hashes para diagnosticar configuración.

La eliminación del PEM del árbol Git no revoca la credencial. Documentar el incidente sin copiar material secreto. Mantener separación entre claves demo y producción. No proporcionar cuentas de terceros ni evadir restricciones de jurisdicción/proveedor con VPN.

## GitHub y cambios

Proponer protección de `main`: PR obligatorio, checks obligatorios, no force-push ni eliminación y revisión independiente para dinero/seguridad. Si el repo es de una persona, no fingir que otra IA equivale a una revisión independiente humana; usar revisión diferida y evidencia reproducible mientras se consigue colaboración.

No modificar workflows o permisos del repo junto con una corrección financiera sin revisión específica. CI de PR sin secretos, sin self-hosted runner compartido con producción y sin descarga/ejecución no controlada de material aportado por terceros. No cambiar la licencia actual por inferir que repo público equivale a software libre.

## Incidentes

Ante exposición de secreto: deshabilitar componente, revocar clave en proveedor, conservar logs saneados, examinar actividad y verificar nueva credencial antes del reinicio. Ante conciliación incierta: bloquear nuevas propuestas de ejecución y conservar reservas. Ante fallo de cancelación: estado UNKNOWN, no CANCELLED. Ningún kill switch promete que el exchange haya cancelado; hace falta confirmación.

Estos controles son requisitos de implementación. Un MD o `TRADING_ENABLED=false` no sustituye la prueba de su cumplimiento.
