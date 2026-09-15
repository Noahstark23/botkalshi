# Validación realizada el 2026-09-15

Entorno local del asistente: Python 3.13.5, Linux. No se ha accedido a una terminal del Droplet.

25 tests de `infra/digitalocean-shadow/tests/test_reporting.py` pasaron. Cubren heartbeat/paquete viejos, futuros, faltantes, IDs distintos, flags peligrosos, errores, conteos, JSON duplicado/no finito, ficheros acotados/symlinks y exportación de borradores sin propagar texto sensible. Durante la prueba está bloqueado socket.connect. El módulo no contiene cliente de red, órdenes ni sender.

`bash -n infra/digitalocean-shadow/install.sh`: aprobado. La corrección posterior de permisos conserva ejecutables: una prueba local con archivos temporales 0600/0700 produjo 0644/0755 al aplicar `chmod a+rX,u+w,go-w`. Se volvió a comprobar la sintaxis Bash tras ese cambio. No se ejecutaron apt, root, systemd o firewall en el servidor. No se repitieron los tests de los motores, los 27 del bootstrap o los seis anteriores del colector. No existe un resultado nuevo de rentabilidad.

Git blob SHA local de archivos comprobados:

- reporting.py: `b422a13b898452c646391740dc5549efbc608928`
- install.sh: `367995f5b1057c981c848cc1d8868e381dc92450`
- tests/test_reporting.py: `c2136e00e3718f87ed6fc0a6318eb36634122f39`

El hash del instalador sustituye al de la revisión previa `2fdde1f83aa6672ad1ee69001785644f6b90d3ad`; no se presenta la prueba estática como una ejecución remota.

Reproducción desde la rama:

```bash
python3 -m unittest discover -s infra/digitalocean-shadow/tests -p test_reporting.py -v
bash -n infra/digitalocean-shadow/install.sh
```

DigitalOcean respondió VM active; Desktop Commander respondió cero dispositivos; el intento de conexión TCP al puerto SSH desde este entorno no obtuvo conexión. Ninguno prueba estado de servicio. WhatsApp, Muse y YouTube siguen sin conexión/envío/publicación. No se escribieron balances en Finances ni se conectaron credenciales de mercado.

Estos tests prueban verificación local y borradores. Queda pendiente aceptación remota, endurecimiento de host, captura real, integración de proveedores y revisión económica.
