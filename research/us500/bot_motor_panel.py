"""Read-only runtime cards and the separate, local US500 paper control link."""

from dataclasses import dataclass
from datetime import datetime, timezone

from motor_status_readonly import ACTIVITY_KINDS, sanitize_snapshot, unknown_status
from runtime_telemetry_contract import parse_runtime_telemetry


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


def is_runtime_action(action):
    if action == "u5:panel":
        return True
    if action.startswith("u5:engine:"):
        return action[len("u5:engine:") :] in (*_BY_KEY, "all")
    parts = action.split(":")
    return (
        len(parts) == 4
        and parts[:2] == ["u5", "blocked"]
        and parts[2] in (*_BY_KEY, "all")
        and parts[3] in ("enable", "pause")
    )


def _snapshot(runtime, now_ms):
    return (
        sanitize_snapshot(runtime, now_ms=now_ms)
        if runtime is not None
        else unknown_status(now_ms, "NOT_CONNECTED")
    )


def _bool(value):
    return "sí" if value is True else "no" if value is False else "desconocido"


def _source(status):
    def iso(at):
        return (
            datetime.fromtimestamp(at / 1000, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            if at is not None
            else "sin fecha"
        )

    if status["fresh"]:
        return (
            "Fuente: runtime técnico del droplet · sólo lectura.\n"
            f"Actualizado: {iso(status['source_at_ms'])} · antigüedad {status['age_ms'] // 1000} s."
        )
    reason = {
        "NOT_CONNECTED": "fuente sin conectar",
        "STALE": "dato vencido",
        "INVALID": "dato inválido",
        "UNAVAILABLE": "consulta fallida",
    }.get(status["reason"], "sin dato vigente")
    return (
        "Fuente: runtime técnico del droplet · sólo lectura.\n"
        f"Consulta: {iso(status['checked_at_ms'])} · {reason}. Estados desconocidos."
    )


def _global(status):
    flags = status["flags"]
    environment = {"production": "producción", "demo": "demo"}.get(
        status["environment"], "desconocido"
    )
    return (
        f"Entorno reportado: {environment}.\n"
        f"Trading configurado: {_bool(flags['trading_enabled'])}. "
        f"Pausa del bot: {_bool(flags['bot_paused'])}.\n"
        f"Pausa por capital: {_bool(flags['capital_paused'])}.\n"
        f"Captura reportada: {_bool(flags['capture_running'])}. "
        f"WebSocket conectado: {_bool(flags['ws_connected'])}."
    )


def _activity(status, key=None):
    activity = status["activity"]
    if not activity["available"]:
        reason = {
            "STALE": "datos vencidos",
            "INVALID": "respuesta inválida",
            "NOT_CONNECTED": "fuente sin conectar",
            "UNAVAILABLE": "consulta no disponible",
        }.get(activity["reason"], "sin dato vigente")
        return f"Actividad registrada: desconocida ({reason}). No indica apagado."
    keys = (key,) if key is not None else tuple(ACTIVITY_KINDS)
    counts = " · ".join(f"{name.upper()}: {activity['counts'][name]}" for name in keys)
    end = datetime.fromtimestamp(
        activity["generated_at_ms"] / 1000, timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"Registros de señales: {counts}.\n"
        f"Ventana: desde {activity['cutoff_utc']} 00:00 UTC hasta {end}.\n"
        f"Consulta agregada hace {activity['age_ms'] // 1000} s; frecuencia máxima: una cada 5 min.\n"
        "Cero registros no significa apagado; registros no prueban ejecución."
    )


def _telemetry(status, now_ms, key=None):
    parsed = parse_runtime_telemetry(status.get("telemetry"), now_ms=now_ms)
    if not parsed["available"]:
        reason = status.get("telemetry_reason", parsed["reason"])
        label = {
            "NOT_INSTALLED": "el runtime todavía no publica el detalle por componente",
            "STALE": "la observación por componente está vencida",
            "INVALID": "la observación por componente fue rechazada",
        }.get(reason, "sin observación por componente")
        return "Telemetría individual: " + label + "."
    source = parsed["source"]
    if key is None:
        return (
            "Telemetría individual disponible en la ficha de cada motor. "
            f"Fuente {source['commit_sha'][:12]} · instancia {source['instance_id'][:8]}."
        )
    flags = parsed["motors"][key]
    lines = [
        "Observación por componente:",
        f"Configurado: {_bool(flags['configured'])}; entradas: {_bool(flags['execution_entry_configured'])}; salidas: {_bool(flags['execution_sell_configured'])}.",
    ]
    for name, component in parsed["components"].items():
        if component["motor"] != key:
            continue
        if component["task_model"] == "callback":
            lines.append(f"{name}: callback dependiente, sin tarea propia.")
        else:
            lines.append(f"{name}: tarea {_bool(component['task_running']['value'])}.")
        returned = component["tick_returned"]
        age = (
            str(max(0, now_ms - returned["observed_at_ms"]) // 1000) + " s"
            if returned["observed_at_ms"] is not None
            else "sin registro"
        )
        lines.append(f"Último retorno: {_bool(returned['value'])}; antigüedad {age}.")
    lines.append(
        "Una tarea viva o un retorno no prueban una operación ni una evaluación exitosa."
    )
    return "\n".join(lines)


def overview(enabled, *, runtime=None, now_ms=None):
    """Configured flags and reported tasks are separate; none grants control."""
    status = _snapshot(runtime, now_ms)
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
                {"text": "Estado general", "callback_data": "u5:engine:all"},
                {"text": "Actualizar estado", "callback_data": "u5:panel"},
            ],
        ]
    )
    lines = []
    for motor in MOTORS:
        observed = status["motors"][motor.key]
        label = "detección configurada" if motor.key == "m1" else "configurado"
        lines.append(
            f"{motor.key.upper()}: {label} {_bool(observed['configured'])} · tarea {_bool(observed['running'])}."
        )
    return (
        "BotKalshi · Todos los motores\n\n"
        + _source(status)
        + "\n\n"
        + _global(status)
        + "\n\n"
        + "\n".join(lines)
        + "\n\n"
        + _activity(status)
        + "\n\nUS500: permiso de simulación "
        + ("habilitado" if enabled else "pausado")
        + ".\nConfiguración no demuestra ejecución.\n"
        + _telemetry(status, now_ms)
        + "\nEste panel no puede activar ni pausar motores reales. "
        + "Habilitar US500 permite replays locales; no inicia una estrategia automática.",
        rows,
    )


def disconnected_detail(key, *, requested_action=None, runtime=None, now_ms=None):
    """Legacy name retained; cards are read-only and old action buttons stay blocked."""
    status = _snapshot(runtime, now_ms)
    if key == "all":
        title = "Estado general del bot"
        description = (
            _global(status)
            + "\n\n"
            + _activity(status)
            + "\n\n"
            + _telemetry(status, now_ms)
        )
        contract = "Las banderas configuradas no prueban que haya operaciones ni tareas ejecutándose."
    else:
        motor = _BY_KEY.get(key)
        if motor is None:
            return None
        title = motor.title
        observed = status["motors"][key]
        task = (
            "en marcha"
            if observed["running"] is True
            else "detenida"
            if observed["running"] is False
            else "desconocida"
        )
        label = "Detección configurada" if key == "m1" else "Configurado"
        execution_label = (
            "Salidas configuradas" if key == "m2" else "Ejecución configurada"
        )
        description = (
            f"{label}: {_bool(observed['configured'])}.\n"
            f"{execution_label}: {_bool(observed['execution_configured'])}.\n"
            f"Tarea reportada: {task}.\n"
        )
        if key == "m2":
            description += "Entradas: sin dato expuesto.\n"
        if key == "m5":
            description += (
                f"M5 habilitado según runtime: {_bool(observed['runtime_enabled'])}.\n"
            )
        if key == "m1":
            description += "La bandera de detección de M1 no demuestra ejecución.\n"
        if key == "rest":
            description += "La captura general no demuestra que la tarea REST de arbitraje esté en marcha.\n"
        if key in ("m6", "m8", "m9") and status["telemetry"] is None:
            description += "El runtime no expone configuración ni tarea actual de este observador.\n"
        if key in ACTIVITY_KINDS:
            description += "\n" + _activity(status, key) + "\n"
        description += "\n" + _telemetry(status, now_ms, key) + "\n"
        description += "\n" + motor.description
        contract = motor.pause_contract
    acknowledgement = ""
    if requested_action is not None:
        acknowledgement = "No se aplicó ningún cambio: este panel es de sólo lectura para motores reales.\n\n"
    return (
        acknowledgement
        + title
        + "\n\n"
        + _source(status)
        + "\n\n"
        + description
        + "\n"
        + contract
        + "\n\nSin controles de activación o pausa real.",
        [
            [
                {
                    "text": "Actualizar estado",
                    "callback_data": "u5:engine:" + key,
                }
            ],
            BACK,
        ],
    )
