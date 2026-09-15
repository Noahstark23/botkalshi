# Validación realizada el 2026-09-15

Entorno local del asistente: Python 3.13.5, Linux. No se ha accedido a una terminal del Droplet.

25 tests de `infra/digitalocean-shadow/tests/test_reporting.py` pasaron. Cubren heartbeat/paquete viejos, futuros, faltantes, IDs distintos, flags peligrosos, errores, conteos, JSON duplicado/no finito, ficheros acotados/symlinks y exportación de borradores sin propagar texto sensible. Durante la prueba está bloqueado socket.connect. El módulo no contiene cliente de red, órdenes ni sender.

`bash -n infra/digitalocean-shadow/install.sh`: aprobado. No se ejecutaron apt, root, systemd o firewall en el servidor. No se repitieron los tests de los motores, los 27 del bootstrap o los seis anteriores del colector. No existe un resultado nuevo de rentabilidad.

Git blob SHA local de archivos probados:

- reporting.py: `b422a13b898452c646391740dc5549efbc608928`
- install.sh: `2fdde1f83aa6672ad1ee69001785644f6b90d3ad`
- tests/test_reporting.py: `c2136e00e3718f87ed6fc0a6318eb36634122f39`

Reproducción desde la rama:

```bash
python3 -m unittest discover -s infra/digitalocean-shadow/tests -p test_reporting.py -v
bash -n infra/digitalocean-shadow/install.sh
```

DigitalOcean respondió VM active; Desktop Commander respondió cero dispositivos; el intento SSH desde este entorno no obtuvo conexión. Ninguno prueba estado de servicio. WhatsApp, Muse y YouTube siguen sin conexión/envío/publicación. No se escribieron balances en Finances ni se conectaron credenciales de mercado.

Estos tests prueban verificación local y borradores. Queda pendiente aceptación remota, endurecimiento de host, captura real, integración de proveedores y revisión económica.
