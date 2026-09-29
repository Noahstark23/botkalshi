# Runtime nativo: standby tecnico

Paquete para Ubuntu 24.04, Python 3.12 y systemd. No necesita Docker, Coolify ni
un servicio pagado adicional. **No activa trading**: ejecuta `src.runner` con
datos reales de produccion, todos los motores apagados y sin simulaciones nuevas.
El prechequeo de la unidad rechaza flags de ejecucion o estudios encendidos.

## Preparacion, sin iniciar ni habilitar la unidad

- Revisar el SHA exacto y la configuracion. Crear usuario de sistema dedicado
  `botkalshi-runtime`, sin login. No reutilizar el usuario ni la DB del colector.
- Preparar el release completo en `/opt/botkalshi-runtime/releases/<SHA>` y crear
  su `.venv` **en Ubuntu**, con `python3.12 -m venv .venv`; no copiar la de macOS.
  Instalar dependencias con `.venv/bin/pip install --require-hashes -r
  infra/digitalocean-runtime/requirements.lock` y verificar `.venv/bin/pip check`.
  El runner importa `src` desde el release mediante WorkingDirectory/PYTHONPATH;
  conservar tambien `scripts/`. El lock fija la resolucion para Linux x86_64 y
  Python 3.12. `current` debe apuntar a ese release revisado.
- Guardar `runtime.env` en `/etc/botkalshi-runtime/`, propiedad de root y modo
  `0600`. Entregar el ID real fuera del repositorio. Guardar el PEM privado como
  `kalshi_private_key.pem`, legible solo por root y el grupo del servicio (`0640`).
  Directorio `root:botkalshi-runtime`, `0750`. No registrar ni imprimir secretos.
- Instalar la unidad como archivo, **sin ejecutar enable/start**. La mera copia
  no inicia procesos. La ausencia de env, PEM o `STANDBY_REVIEWED` impide el arranque.
  El archivo de revision se crea solo despues de completar el checklist inferior;
  no es prueba de autenticacion ni autorizacion para trading.
- systemd mantiene DB en `/var/lib/botkalshi-runtime` y logs en
  `/var/log/botkalshi-runtime`, fuera del release. `BindPaths` presenta esos logs
  como `/app/logs` solo dentro de la unidad: no mover, borrar ni sobrescribir
  directorios `/app` existentes. Validar la unidad y su montaje en el host destino.

## Gate obligatorio antes de cualquier inicio

- [ ] Codigo revisado y verificacion proporcional del release, no solo CI antiguo.
- [ ] No hay otro proceso que use la misma DB, identidad operativa o puerto 18080.
- [ ] ID/PEM utilizables, configuracion efectiva validada sin exponer secretos.
- [ ] Autenticacion, saldo, posiciones y ordenes verificados con consultas de lectura;
      una lectura incompleta o desconocida no se interpreta como cuenta vacia.
- [ ] Todos los flags indicados siguen apagados; Odds API y Telegram vacios.
- [ ] DB nueva identificada como nueva, o respaldo coherente del historial existente
      antes de reutilizarlo. No reemplazar historial con una DB vacia silenciosamente.
- [ ] Puerto exclusivamente `127.0.0.1:18080`: los endpoints administrativos no tienen
      autenticacion; no abrir firewall ni publicar proxy hacia ellos.
- [ ] Recursos y reversibilidad revisados. En 1 GB no hay capacidad certificada:
      limites iniciales 500 MiB suaves / 650 MiB duros, 256 MiB swap, CPU 80 %.
      No aumentar plan ni limites automaticamente. Revisar servicio y carga existentes.

## Comprobacion y recuperacion

Una vez autorizado y completado el gate, comprobar localmente `/health`, `/ready`
y `/status`, captura/WS real, saldo fresco, errores, permisos de DB/logs y flags
efectivos. Observar al menos 15 minutos `MemoryCurrent`, `MemoryPeak`, swap y
`NRestarts` de systemd. Salud HTTP **no prueba trading ni beneficio**. El monitor
interno de memoria queda apagado porque presupone cgroups de Docker.

El runner escribe SQLite y logs: reconcilia estados y registra settlements locales,
pero con este perfil no coloca ni cancela ordenes en Kalshi. Si fallan autenticacion,
montaje, captura, persistencia o limites, detener el servicio y revisar; no hacer
bucles manuales de restart. La unidad limita reintentos a tres en diez minutos.
Volver al release previamente verificado conservando DB/logs; una restauracion de
DB exige revision separada. No tocar servicios ajenos ni el colector existente.

Resultado posible de este paquete: **servicio tecnico conectado y sin ordenes**.
No equivale a bot operando dinero real y no incluye instrucciones para activarlo.
