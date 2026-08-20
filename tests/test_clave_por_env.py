"""
Entrega de la clave privada por ENV VAR — las tres formas en que un panel guarda un PEM.

POR QUÉ EXISTE (incidente 2026-08-20, bot CAÍDO en crash-loop): al sacar la clave de la
imagen (#248) el bot no pudo arrancar más — en una app COMPOSE de Coolify los file mounts
del panel se ignoran y el volumen que la UI ofrece no existe en el host, así que NO hay
forma de poner un archivo en /app/secrets. La env var es la única vía de entrega, pero un
PEM es multilínea y cada panel lo guarda distinto. Un segundo crash-loop por un `\\n`
literal cuesta horas de bot caído: por eso las tres formas se aceptan y se fijan acá.

Lo que se pinea es el COMPORTAMIENTO DESEADO:
  - las tres formas producen la MISMA clave
  - el contenido GANA sobre el archivo (precedencia explícita)
  - sin ninguna de las dos, el boot ROMPE con un mensaje que dice cómo arreglarlo
  - el material NUNCA aparece en un repr/str de Settings ni en un mensaje de error
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.auth.signer import KalshiSigner, cargar_clave_pem, normalizar_pem

API_KEY_ID = "test-key-id-12345"


@pytest.fixture(scope="module")
def pem_real() -> str:
    """Un PEM RSA-2048 de verdad (generado, jamás uno del repo)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _numero_modular(pem_bytes: bytes) -> int:
    """Identidad de la clave — para probar que dos formatos dan la MISMA clave."""
    key = serialization.load_pem_private_key(pem_bytes, password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return key.public_key().public_numbers().n


# ---------------------------------------------------------------- normalizar_pem


def test_saltos_reales_pasan_intactos(pem_real):
    """Forma 1: el PEM tal cual sale del archivo."""
    assert _numero_modular(normalizar_pem(pem_real)) == _numero_modular(pem_real.encode())


def test_barra_n_literal_se_convierte(pem_real):
    """Forma 2: el panel web guardó '\\n' como DOS caracteres — el caso más común."""
    escapado = pem_real.replace("\n", "\\n")
    assert "\\n" in escapado and escapado.count("\n") == 0
    assert _numero_modular(normalizar_pem(escapado)) == _numero_modular(pem_real.encode())


def test_crlf_escapado_tambien(pem_real):
    """Variante de la forma 2: paneles que escapan CRLF."""
    escapado = pem_real.replace("\n", "\\r\\n")
    assert _numero_modular(normalizar_pem(escapado)) == _numero_modular(pem_real.encode())


def test_base64_del_pem_entero(pem_real):
    """Forma 3: el operador prefirió evitar el multilínea y pegó base64."""
    b64 = base64.b64encode(pem_real.encode()).decode()
    assert _numero_modular(normalizar_pem(b64)) == _numero_modular(pem_real.encode())


def test_base64_con_saltos_tambien(pem_real):
    """El base64 pegado desde una terminal suele venir cortado en líneas."""
    b64 = base64.b64encode(pem_real.encode()).decode()
    troceado = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    assert _numero_modular(normalizar_pem(troceado)) == _numero_modular(pem_real.encode())


def test_vacio_es_error_explicito():
    with pytest.raises(ValueError, match="vac"):
        normalizar_pem("   \n  ")


def test_basura_que_no_es_pem_ni_base64():
    """Control: no se adivina. Sin cabecera y sin base64 válido → error claro."""
    with pytest.raises(ValueError, match="BEGIN"):
        normalizar_pem("esto no es una clave, es una frase")


def test_base64_valido_que_no_esconde_un_pem():
    """Control fino: decodifica pero no es PEM → tampoco se acepta."""
    b64 = base64.b64encode(b"hola mundo que no es un pem").decode()
    with pytest.raises(ValueError, match="no es un PEM|BEGIN"):
        normalizar_pem(b64)


# ---------------------------------------------------------------- validación de la clave


def test_clave_no_rsa_se_rechaza():
    """Una EC válida es PEM válido pero Kalshi firma RSA-PSS: tiene que romper."""
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError, match="no es RSA"):
        cargar_clave_pem(pem, origen="test")


def test_clave_corta_se_rechaza():
    """1024 bits parsea perfecto y es inseguro: el mínimo es 2048."""
    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError, match="muy pequeño"):
        cargar_clave_pem(pem, origen="test")


def test_el_error_no_filtra_el_material():
    """El material JAMÁS entra al mensaje: un error termina en los logs."""
    secreto = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKxSecretoQueNoDebeAparecer\n"
    with pytest.raises(ValueError) as exc:
        cargar_clave_pem(secreto, origen="env")
    assert "SecretoQueNoDebeAparecer" not in str(exc.value)


# ---------------------------------------------------------------- signer: precedencia


def test_signer_acepta_pem_por_contenido(pem_real):
    signer = KalshiSigner(api_key_id=API_KEY_ID, private_key_pem=pem_real)
    assert signer.sign("GET", "/trade-api/v2/portfolio/balance")


def test_signer_pem_gana_sobre_el_archivo(tmp_path, pem_real):
    """PRECEDENCIA: con las dos vías presentes manda el contenido del env.

    Es lo que permite arreglar producción sin tocar el archivo montado."""
    otra = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    archivo = tmp_path / "otra.pem"
    archivo.write_bytes(
        otra.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    signer = KalshiSigner(private_key_path=archivo, api_key_id=API_KEY_ID, private_key_pem=pem_real)
    esperado = _numero_modular(pem_real.encode())
    assert signer._private_key.public_key().public_numbers().n == esperado


def test_signer_sin_ninguna_via_es_error():
    with pytest.raises(ValueError, match="private_key_pem|private_key_path"):
        KalshiSigner(api_key_id=API_KEY_ID)


def test_archivo_sigue_funcionando(tmp_path, pem_real):
    """Regresión: la vía histórica (archivo) no se rompe."""
    archivo = tmp_path / "k.pem"
    archivo.write_text(pem_real)
    signer = KalshiSigner(archivo, API_KEY_ID)
    assert signer.sign("GET", "/x")


# ---------------------------------------------------------------- Settings


def _settings(monkeypatch, tmp_path, **extra):
    """Settings mínimo, aislado del .env real del repo."""
    from src.utils.config import Settings

    monkeypatch.setenv("KALSHI_API_KEY_ID", "0123456789abcdef")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_settings_arranca_con_la_clave_en_el_env(monkeypatch, tmp_path, pem_real):
    """EL CASO DEL INCIDENTE: sin archivo en disco, el boot tiene que salir bien."""
    s = _settings(
        monkeypatch,
        tmp_path,
        KALSHI_PRIVATE_KEY=pem_real,
        KALSHI_PRIVATE_KEY_PATH=str(tmp_path / "no-existe.pem"),
    )
    assert s.clave_privada_env() is not None


def test_settings_rompe_sin_clave_por_ninguna_via(monkeypatch, tmp_path):
    """Fail-fast del boot, con un mensaje que dice las DOS salidas."""
    with pytest.raises(Exception) as exc:
        _settings(
            monkeypatch,
            tmp_path,
            KALSHI_PRIVATE_KEY="",
            KALSHI_PRIVATE_KEY_PATH=str(tmp_path / "no-existe.pem"),
        )
    msg = str(exc.value)
    assert "KALSHI_PRIVATE_KEY" in msg and "no-existe.pem" in msg


def test_settings_rompe_con_pem_mal_pegado(monkeypatch, tmp_path):
    """Un PEM roto rompe el BOOT, no el primer request (fail-fast)."""
    with pytest.raises(Exception, match="KALSHI_PRIVATE_KEY"):
        _settings(
            monkeypatch,
            tmp_path,
            KALSHI_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nrota\n",
            KALSHI_PRIVATE_KEY_PATH=str(tmp_path / "no-existe.pem"),
        )


def test_la_clave_no_aparece_en_el_repr_de_settings(monkeypatch, tmp_path, pem_real):
    """SecretStr: ni repr ni str ni model_dump() pueden escupir el material.

    Un Settings se loguea entero con más facilidad de la que uno cree."""
    s = _settings(
        monkeypatch,
        tmp_path,
        KALSHI_PRIVATE_KEY=pem_real,
        KALSHI_PRIVATE_KEY_PATH=str(tmp_path / "no-existe.pem"),
    )
    cuerpo = pem_real.splitlines()[1][:20]  # un trozo del material, no la cabecera
    for rendido in (repr(s), str(s), str(s.model_dump())):
        assert cuerpo not in rendido
