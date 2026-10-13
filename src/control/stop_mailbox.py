"""Dos buzones de capacidad fija para parada/consulta; sin sockets ni poller.

Directorios preexistentes de un solo escritor, lector por grupo/ACL. No concede
permisos, crea claves ni monta rutas. El despliegue debe validar UID/GID reales
(incluido PrivateUsers); este protocolo no relaja el aislamiento del panel.
"""

import os
import stat
from pathlib import Path

from src.control.entry_stop import StopRequest, verify_receipt
from src.control.wire import MAX_BYTES, ControlError, _integer, _require


class StopMailbox:
    def __init__(self, directory, *, owner_uid: int):
        _require(_integer(owner_uid), "INVALID_MAILBOX_OWNER")
        self.path = Path(directory).absolute()
        self.owner_uid = owner_uid
        self.identity = self._directory_identity()

    def _directory_identity(self):
        info = self.path.lstat()
        _require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == self.owner_uid
            and info.st_mode & 0o027 == 0,
            "INSECURE_MAILBOX_DIRECTORY",
        )
        return info.st_dev, info.st_ino

    def _open(self):
        _require(self._directory_identity() == self.identity, "MAILBOX_REPLACED")
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != self.identity:
            os.close(fd)
            raise ControlError("MAILBOX_REPLACED")
        return fd

    @staticmethod
    def _name(operation):
        _require(operation in ("stop", "status"), "INVALID_STOP_OPERATION")
        return operation + ".json"

    def read(self, operation):
        name, directory = self._name(operation), self._open()
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(fd)
                _require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1
                    and info.st_uid == self.owner_uid
                    and info.st_mode & 0o027 == 0
                    and info.st_size <= MAX_BYTES,
                    "INVALID_MAILBOX_FILE",
                )
                value = os.read(fd, MAX_BYTES + 1)
                _require(len(value) <= MAX_BYTES, "PAYLOAD_TOO_LARGE")
                return value
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def publish(self, operation, wire):
        _require(type(wire) is bytes and 0 < len(wire) <= MAX_BYTES, "INVALID_ENVELOPE")
        name, directory = self._name(operation), self._open()
        temporary = "." + name + ".pending"
        try:
            _require(os.geteuid() == self.owner_uid, "MAILBOX_NOT_WRITER")
            # Un temporal dejado por un crash requiere resolución explícita.
            # No se pisa ni se acumula una lista ilimitada de temporales.
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o640,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(wire)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            except Exception:
                # Persistencia incierta: no borrar evidencias ni afirmar entrega.
                raise ControlError("MAILBOX_PUBLISH_UNCONFIRMED") from None
        finally:
            os.close(directory)


def consume_once(requests: StopMailbox, receipts: StopMailbox, channel):
    """Invocación explícita en el dueño del ledger; máximo dos sobres por ciclo.

    Sólo stop/status, nunca ejecutores. Un fallo no impide procesar la otra vía;
    no emite éxito ni texto arbitrario. La repetición sólo permite el latch idempotente;
    no existe una operación financiera en este protocolo.
    """
    results = {}
    for operation in ("stop", "status"):
        try:
            wire = requests.read(operation)
            if wire is None:
                results[operation] = "EMPTY"
                continue
            request = StopRequest.parse(channel.auth.open("entry-stop-request-v1", wire))
            _require(request.operation == operation, "WRONG_MAILBOX_OPERATION")
            now = channel.adapter.clock()
            _require(_integer(now, 946684800000), "INVALID_TIME")
            if now >= request.expires_at_ms:
                results[operation] = "EXPIRED"
                continue
            request.authorize(channel.adapter.binding, now)
            existing = receipts.read(operation)
            if existing is not None:
                try:
                    verify_receipt(channel.auth, request, existing, now_ms=channel.adapter.clock())
                except Exception:
                    pass
                else:
                    results[operation] = "RECEIPT_PRESENT"
                    continue
            receipts.publish(operation, channel.handle(wire))
            results[operation] = "RECEIPT_WRITTEN"
        except Exception:
            results[operation] = "UNCONFIRMED"
    return results
