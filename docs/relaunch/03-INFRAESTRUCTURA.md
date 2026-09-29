# 03. Infraestructura y despliegue

## Perfiles

| Perfil | Datos | Red y secretos | Órdenes |
|---|---|---|---|
| OFFLINE | Fixtures sintéticas / snapshot autorizado | Sin red ni claves | Imposibles |
| SHADOW_READONLY | Mercado real autorizado | Lecturas allowlist; secretos mínimos si imprescindibles | Bloqueadas por transporte y arquitectura |
| DEMO_MECHANICS | Demo con credenciales separadas | Solo hosts demo | Fase posterior, requiere autorización específica |
| LIVE | Producción | No provisionado en esta entrega | Fuera de alcance |

Estos nombres son diseño, no flags que ya entienda `src.runner`. El perfil M5 F1 existente requiere `KALSHI_ENV=production` para observar mercado real y ejecución apagada. No sustituirlo por demo sin revisar validación de configuración. Es producción de DATOS, no permiso de trading.

## Preparación local sin arranque del bot

Usar una copia del repositorio y rama aislada. Si ya existe, conservar cambios del usuario. Comandos de inspección:

```bash
git status --short
git rev-parse HEAD
git diff --stat
python3.12 --version
```

Para un checkout nuevo, clonar desde el repositorio conocido y seleccionar la rama del PR de esta entrega. No usar `reset --hard`, no sobrescribir `.env` ni arrancar el compose raíz. Instalar dependencias solo en un venv o contenedor aislado; revisar primero hooks, scripts y archivos de configuración. El lock reproducible es un entregable de PR separado, no una propiedad demostrada del pyproject actual.

Primero ejecutar fixtures y regresiones con red externa bloqueada, permitiendo únicamente loopback para pruebas WS locales. No cargar automáticamente el `.env` del operador en pytest. Cualquier intento de conexión real debe fallar y quedar registrado.

## Topología objetivo de bajo costo

Primera opción: el equipo local ya disponible, sin contratar un VPS nuevo. Python 3.12; imagen Docker construida desde lock; SQLite en disco local persistente; exportaciones separadas y saneadas. Objetivo inicial de recursos del servicio: 1 vCPU y 1 GiB, hasta cinco tickers M5. Son presupuestos a medir, no benchmarks ni mínimos universales. Sin GPU ni modelo local exigidos.

Para el contenedor de investigación: usuario no root, rootfs de solo lectura, tmpfs acotado, `cap_drop: [ALL]`, `no-new-privileges`, sin Docker socket, sin host networking y sin mounts de carpetas personales. OFFLINE lleva `network_mode: none`; SHADOW requiere una política de egress aprobada. No exponer el panel a Internet; bind de host a 127.0.0.1 en un puerto distinto de otros servicios, por ejemplo 18081. No incluir secretos en Dockerfile, build args, imagen, repositorio o logs.

Una sola definición de dependencias y un digest de imagen verificado. El futuro compose endurecido será explícito y separado; no se incluye un `docker compose up` sobre un entrypoint inexistente. La imagen exacta, su digest y el comando de arranque se fijan después de implementar y probar el perfil seguro.

## Puerta antes de datos autenticados

Probar que ninguna combinación de flags permite POST/PUT/PATCH/DELETE de trading a Kalshi. El proceso no importa/inicializa ejecutores, gestores de salidas o auto-settlement con escritura. Las lecturas públicas REST son preferibles cuando bastan. WS requiere autenticación incluso para canales de mercado: no se promete WS sin claves.

Antes de habilitar una nueva clave, verificar la revocación de cualquier clave expuesta históricamente. Inyectarla localmente como archivo fuera del repo; nunca pedirla por chat. Si el proveedor no ofrece una credencial realmente limitada a lectura, documentar el riesgo residual y no suponer que el nombre read-only lo resuelve.

## Despliegue y rollback propuestos

Tras CI, prueba de arranque, snapshot/restore y aprobación operativa: detener instancia de investigación, cerrar cohorte, crear backup consistente, aplicar migraciones sobre copia, iniciar nueva imagen con ejecución bloqueada y verificar salud/cobertura. Si falla, detener nuevas observaciones, restaurar snapshot compatible y versión anterior del laboratorio. No degradar esquema destructivamente ni cambiar flags para que arranque.

No desplegar en el host de Nortex sin revisar capacidad y aislamiento. Coolify es reutilizable si sigue disponible; no se verificó su estado ni se provisionó aquí.
