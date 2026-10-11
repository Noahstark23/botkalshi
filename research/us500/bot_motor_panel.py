"""Catálogo del panel: no importa motores, entornos ni clientes de producción.

La disponibilidad describe este receptor, nunca el estado del bot remoto.
Sólo US500 posee un adaptador conectado, limitado al permiso de replay paper.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Motor:
    key: str
    title: str
    description: str
    pause_contract: str


MOTORS = (
    Motor(
        "m1",
        "M1 · Arbitraje binario",
        "Detecta diferencias entre los dos lados de un mercado.",
        "Pausar entradas debe conservar los contratos ya comprados y su conciliación. La activación de M1 sigue pendiente de revisión.",
    ),
    Motor(
        "m2",
        "M2 · Consenso de cuotas",
        "Calcula precios de referencia y gestiona entradas y salidas por separado.",
        "Pausar entradas debe mantener las salidas y los datos que consume M5.",
    ),
    Motor(
        "m3",
        "M3 · Gestión de salidas",
        "Gestiona posiciones existentes; no es un motor de nuevas compras.",
        "Desactivar la gestión de salidas requiere revisar primero la exposición. No debe tratarse como una pausa de entradas.",
    ),
    Motor(
        "m5",
        "M5 · Market maker",
        "Genera cotizaciones usando el precio de referencia de M2.",
        "Pausar cotizaciones debe conservar inventario, reservas y conciliación. Las órdenes abiertas no se consideran canceladas sin confirmación.",
    ),
    Motor(
        "m6",
        "M6 · Movimiento de cuotas",
        "Observador dentro del ciclo de M2. No tiene ejecutor de órdenes.",
        "El futuro control corresponde a observación, no a trading real.",
    ),
    Motor(
        "rest",
        "REST · Arbitraje múltiple",
        "Observa arbitrajes entre varios resultados.",
        "Pausar nuevas entradas no debe detener el seguimiento de liquidaciones del bot.",
    ),
    Motor(
        "m8",
        "M8 · Flujo de órdenes",
        "Observador del libro de órdenes. No tiene ejecutor de órdenes.",
        "El futuro control corresponde a observación, no a trading real.",
    ),
    Motor(
        "m9",
        "M9 · Mercados relacionados",
        "Observador de movimientos entre mercados. No tiene ejecutor de órdenes.",
        "El futuro control corresponde a observación, no a trading real.",
    ),
)
_BY_KEY = {motor.key: motor for motor in MOTORS}
BACK = [{"text": "← Todos los motores", "callback_data": "u5:panel"}]


def overview(enabled):
    """Texto y teclado; no representa flags ni salud de procesos remotos."""
    rows = [
        [
            {"text": motor.title, "callback_data": "u5:engine:" + motor.key}
            for motor in MOTORS[index : index + 2]
        ]
        for index in range(0, len(MOTORS), 2)
    ]
    rows.extend(
        [
            [{"text": "US500 · Simulación", "callback_data": "u5:menu"}],
            [
                {"text": "Control general", "callback_data": "u5:engine:all"},
                {"text": "Actualizar estado", "callback_data": "u5:panel"},
            ],
        ]
    )
    return (
        "BotKalshi · Todos los motores\n\n"
        "US500: permiso de simulación "
        + ("habilitado" if enabled else "pausado")
        + ".\nM1, M2, M3, M5, M6, REST, M8 y M9: conexión pendiente.\n\n"
        "Elegí un motor para ver sus controles.\n"
        "Este panel no está conectado al trading real y no consulta su estado. "
        "Habilitar US500 permite replays locales; no inicia una estrategia automática.",
        rows,
    )


def disconnected_detail(key, *, requested_action=None):
    """Los botones sin adaptador explican el bloqueo, jamás fingen una operación."""
    if key == "all":
        title = "Control general del bot"
        description = "La conexión de los motores al panel está pendiente."
        contract = (
            "Una futura pausa general debe frenar nuevas entradas y mantener "
            "salidas, reservas y conciliación. No equivale a cerrar posiciones."
        )
    else:
        motor = _BY_KEY.get(key)
        if motor is None:
            return None
        title, description, contract = (
            motor.title,
            motor.description,
            motor.pause_contract,
        )
    acknowledgement = ""
    if requested_action is not None:
        acknowledgement = "No se aplicó ningún cambio: falta conectar este control.\n\n"
    return (
        acknowledgement
        + title
        + "\nConexión al panel: pendiente.\nEstado del proceso: no consultado.\n\n"
        + description
        + "\n"
        + contract,
        [
            [
                {
                    "text": "🔒 Activar · sin conexión",
                    "callback_data": "u5:blocked:" + key + ":enable",
                },
                {
                    "text": "🔒 Desactivar · sin conexión",
                    "callback_data": "u5:blocked:" + key + ":pause",
                },
            ],
            BACK,
        ],
    )
