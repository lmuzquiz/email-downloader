#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""
Reanuda el backup de un buzon de correo.
Salta correos ya descargados y continua donde se quedo.
Reconecta automaticamente si la conexion se cae.

Uso:
    python scripts/resume.py <dominio> <nombre-cuenta>

Ejemplo:
    python scripts/resume.py <dominio> <cuenta>

El dominio y nombre-cuenta deben coincidir con una entrada en config/cuentas.yaml.
Los archivos se guardan en data/<dominio>/<nombre-cuenta>/.
"""

import base64
import imaplib
import email
import os
import re
import sys
from pathlib import Path
from email.header import decode_header

EXIT_OK = 0
EXIT_PARTIAL = 1


def emit_fail_result(message=None):
    """Imprime RESULT line con status=fail y sale con EXIT_PARTIAL.

    Para fallos antes de empezar el loop de carpetas (config, password,
    auth IMAP) — asi el orquestador no tiene que parsear el stdout para
    saber que algo salio mal: ve exit != 0 + RESULT status=fail.
    """
    if message:
        print(message, file=sys.stderr)
    print("RESULT status=fail carpetas_total=0 carpetas_saltadas=0 "
          "correos_nuevos=0 correos_fallidos=0 reconexiones_fallidas=0")
    sys.exit(EXIT_PARTIAL)


def imap_utf7_decode(s):
    """Decodifica modified UTF-7 (RFC 3501 sec 5.1.3) a unicode.

    IMAP no usa UTF-8 para nombres de mailbox: usa una variante de UTF-7
    que reemplaza '/' por ',' y delimita secciones con '&' y '-'.
    Sin esto, carpetas con acentos/ñ quedan ilegibles o no se pueden
    seleccionar.
    """
    if isinstance(s, (bytes, bytearray)):
        s = s.decode("ascii", errors="replace")
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "&":
            j = s.find("-", i + 1)
            if j == -1:
                out.append(s[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                segment = s[i + 1:j].replace(",", "/")
                pad = (-len(segment)) % 4
                try:
                    out.append(base64.b64decode(segment + "=" * pad).decode("utf-16-be"))
                except Exception:
                    out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def imap_utf7_encode(s):
    """Codifica unicode a modified UTF-7 (RFC 3501)."""
    out = []
    buf = []

    def flush():
        if not buf:
            return
        encoded = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
        out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
        buf.clear()

    for c in s:
        cp = ord(c)
        if 0x20 <= cp <= 0x7e:
            flush()
            if c == "&":
                out.append("&-")
            else:
                out.append(c)
        else:
            buf.append(c)
    flush()
    return "".join(out)

try:
    import yaml
except ImportError:
    print("Falta PyYAML. Ejecuta este script con uv: ./scripts/resume.py o uv run scripts/resume.py")
    sys.exit(1)


CONFIG_FILE = Path(__file__).parent.parent / "config" / "cuentas.yaml"
DATA_DIR = Path(__file__).parent.parent / "data"

# Timeout en segundos para operaciones IMAP. Si la red sufre particion silenciosa,
# el socket TCP cuelga indefinidamente sin esto. 10 minutos cubre fetches grandes
# (correos pesados con adjuntos) sin colgar para siempre.
IMAP_TIMEOUT_SECONDS = 600


def load_account(domain, account_name):
    """Carga la configuracion de una cuenta desde cuentas.yaml."""
    if not CONFIG_FILE.exists():
        emit_fail_result(
            f"No se encontro {CONFIG_FILE}. "
            "Copia config/cuentas.example.yaml a config/cuentas.yaml y llena los datos."
        )

    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    for dom in config.get("dominios", []) or []:
        if dom["dominio"] == domain:
            for cuenta in dom.get("cuentas") or []:
                if cuenta["nombre"] == account_name:
                    cuenta["imap_server"] = dom["imap_server"]
                    cuenta["imap_port"] = dom["imap_port"]
                    return cuenta

            available = [c["nombre"] for c in (dom.get("cuentas") or [])]
            emit_fail_result(
                f"Cuenta '{account_name}' no encontrada en dominio '{domain}'. "
                f"Disponibles en {domain}: {', '.join(available)}"
            )

    available_domains = [d["dominio"] for d in (config.get("dominios") or [])]
    emit_fail_result(
        f"Dominio '{domain}' no encontrado en {CONFIG_FILE}. "
        f"Disponibles: {', '.join(available_domains)}"
    )


def read_password(password_file):
    """Lee la contrasena desde un archivo temporal.

    rstrip('\\r\\n') solo quita saltos de linea finales (los introduce
    'echo'), preservando espacios u otros caracteres legitimos.
    """
    path = Path(password_file)
    if not path.exists():
        emit_fail_result(
            f"No se encontro el archivo de contrasena: {password_file}. "
            f"Crear el archivo con: echo 'tu-contrasena' > {password_file}"
        )
    return path.read_text().rstrip("\r\n")


def parse_folder_response(folder_raw):
    """Devuelve (nombre_imap, nombre_display) o None si no se puede parsear.

    nombre_imap es el string en modified UTF-7 que se le pasa a mail.select().
    nombre_display es la version unicode legible para humanos / disco.

    imaplib.list() puede devolver:
    - bytes: ej. b'(\\HasNoChildren) "." "INBOX"'
    - tuple: cuando el nombre incluye un IMAP literal (caracteres no ASCII en
      el flag o algun raro encoding por servidor), la libreria devuelve una
      tupla cuyo ultimo elemento son los bytes del nombre real.
    """
    if isinstance(folder_raw, tuple):
        if len(folder_raw) >= 2 and isinstance(folder_raw[-1], (bytes, bytearray)):
            imap_name = folder_raw[-1].decode("ascii", errors="replace")
            return imap_name, imap_utf7_decode(imap_name)
        return None
    if isinstance(folder_raw, (bytes, bytearray)):
        line = folder_raw.decode("ascii", errors="replace")
        match = re.search(r'"([^"]*)" "?([^"]*)"?$', line)
        if not match:
            return None
        _, folder_name = match.groups()
        imap_name = folder_name.strip('"')
        return imap_name, imap_utf7_decode(imap_name)
    return None


def safe_filename(name, max_len=80):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.strip('. ')
    return name[:max_len] if name else "sin_asunto"


def decode_subject(msg):
    raw = msg.get("Subject", "sin_asunto")
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def main():
    if len(sys.argv) != 3:
        emit_fail_result(
            "Uso: ./scripts/resume.py <dominio> <nombre-cuenta>\n"
            "Ejemplo: ./scripts/resume.py <dominio> <cuenta>"
        )

    domain = sys.argv[1]
    account_name = sys.argv[2]
    cuenta = load_account(domain, account_name)
    password = read_password(cuenta["password_file"])

    backup_dir = DATA_DIR / domain / account_name

    print(f"Conectando a {cuenta['imap_server']}...")
    try:
        mail = imaplib.IMAP4_SSL(cuenta["imap_server"], cuenta["imap_port"], timeout=IMAP_TIMEOUT_SECONDS)
        mail.login(cuenta["email"], password)
    except Exception as e:
        emit_fail_result(f"Error conectando/autenticando a IMAP: {e}")
    print(f"Autenticacion exitosa: {cuenta['email']}\n")

    status, folders = mail.list()
    if status != "OK":
        emit_fail_result("Error listando carpetas")

    total_descargados = 0
    total_saltados = 0
    carpetas_total = 0
    carpetas_saltadas = 0
    correos_fallidos = 0
    reconexiones_fallidas = 0

    uid_marker = backup_dir / ".uid-format"
    abort_after_folder = False

    for folder_raw in folders:
        if abort_after_folder:
            break
        parsed = parse_folder_response(folder_raw)
        if not parsed:
            print(f"  ! Carpeta no parseable, saltada: {folder_raw!r}")
            carpetas_total += 1
            carpetas_saltadas += 1
            continue
        imap_name, folder_name = parsed
        carpetas_total += 1

        status, data = mail.select(f'"{imap_name}"', readonly=True)
        if status != "OK":
            print(f"  ! No se pudo abrir: {folder_name}")
            carpetas_saltadas += 1
            continue

        try:
            msg_count = int(data[0])
        except (TypeError, ValueError, IndexError) as e:
            print(f"  ! Respuesta inesperada de SELECT en {folder_name}: {e}")
            carpetas_saltadas += 1
            continue
        print(f"  {folder_name} -- {msg_count} correos")

        if msg_count == 0:
            continue

        safe_folder = safe_filename(folder_name, max_len=100)
        folder_dir = backup_dir / safe_folder
        folder_dir.mkdir(parents=True, exist_ok=True)

        for orphan in folder_dir.glob("*.eml.tmp"):
            try:
                orphan.unlink()
            except OSError:
                pass

        # Marker .uid-format al nivel de cuenta. Si NO existe, los archivos
        # presentes son legacy (sequence-number naming) y NO entran al set;
        # se re-descargan con UID nuevo (puede generar duplicados con los
        # legacy, intencional). Si SI existe, todos los archivos son UIDs y
        # se incluyen en el set para dedup.
        if uid_marker.exists():
            existing_uids = set()
            for f in folder_dir.glob("*.eml"):
                name = f.name
                if "_" in name:
                    prefix = name.split("_", 1)[0]
                    if prefix.isdigit():
                        existing_uids.add(prefix)
        else:
            existing_uids = set()
            legacy_count = sum(1 for _ in folder_dir.glob("*.eml"))
            if legacy_count:
                print(f"  Detectados {legacy_count} archivos legacy en {folder_name}; "
                      "se ignoran y se re-descargan con UID.")

        status, data = mail.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            print(f"  ! No se pudo listar UIDs en: {folder_name}")
            carpetas_saltadas += 1
            continue

        uids = data[0].split()
        folder_descargados = 0

        for i, uid in enumerate(uids, 1):
            uid_str = uid.decode("ascii")

            if uid_str in existing_uids:
                total_saltados += 1
                if i % 5000 == 0:
                    print(f"    Saltando {i}/{len(uids)} (ya descargado)")
                continue

            try:
                status, msg_data = mail.uid("FETCH", uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    correos_fallidos += 1
                    continue

                raw_email = msg_data[0][1]
                if not isinstance(raw_email, (bytes, bytearray)):
                    correos_fallidos += 1
                    continue

                msg = email.message_from_bytes(raw_email)

                subject = decode_subject(msg)
                filename = f"{uid_str}_{safe_filename(subject)}.eml"
                filepath = folder_dir / filename
                tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")

                tmp_path.write_bytes(raw_email)
                os.replace(tmp_path, filepath)

                if not uid_marker.exists():
                    uid_marker.touch()

                total_descargados += 1
                folder_descargados += 1
                existing_uids.add(uid_str)

                if folder_descargados % 50 == 0:
                    print(f"    Descargados {folder_descargados} nuevos ({i}/{len(uids)})")

            except Exception as e:
                print(f"    ! Error en correo UID {uid_str}: {e}")
                correos_fallidos += 1
                if "Broken pipe" in str(e) or "EOF" in str(e):
                    print("    Reconectando...")
                    try:
                        mail = imaplib.IMAP4_SSL(cuenta["imap_server"], cuenta["imap_port"], timeout=IMAP_TIMEOUT_SECONDS)
                        mail.login(cuenta["email"], password)
                        mail.select(f'"{imap_name}"', readonly=True)
                        print("    Reconexion exitosa")
                    except Exception as re_err:
                        print(f"    ! No se pudo reconectar: {re_err}")
                        reconexiones_fallidas += 1
                        # Abortar tanto el inner loop como el outer; sin esto la
                        # siguiente carpeta intentaria mail.select() sobre socket
                        # muerto y crashearia sin imprimir RESULT.
                        abort_after_folder = True
                        break
                continue

        if folder_descargados > 0:
            print(f"    {folder_descargados} correos nuevos descargados")

    try:
        mail.logout()
    except Exception:
        pass

    print(f"\nResumen:")
    print(f"  Nuevos descargados: {total_descargados}")
    print(f"  Ya existentes (saltados): {total_saltados}")
    print(f"  Ubicacion: {backup_dir}")

    status_str = "ok" if (carpetas_saltadas == 0 and correos_fallidos == 0 and reconexiones_fallidas == 0) else "partial"
    print(
        f"RESULT status={status_str}"
        f" carpetas_total={carpetas_total}"
        f" carpetas_saltadas={carpetas_saltadas}"
        f" correos_nuevos={total_descargados}"
        f" correos_fallidos={correos_fallidos}"
        f" reconexiones_fallidas={reconexiones_fallidas}"
    )

    sys.exit(EXIT_OK if status_str == "ok" else EXIT_PARTIAL)


if __name__ == "__main__":
    main()
