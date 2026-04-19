#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""
Backup completo de un buzon de correo via IMAP.
Descarga cada correo como archivo .eml individual, organizado por carpeta.

Uso:
    python scripts/backup.py <dominio> <nombre-cuenta>

Ejemplo:
    python scripts/backup.py <dominio> <cuenta>

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

try:
    import yaml
except ImportError:
    print("Falta PyYAML. Ejecuta este script con uv: ./scripts/backup.py o uv run scripts/backup.py")
    sys.exit(1)


def imap_utf7_decode(s):
    """Decodifica modified UTF-7 (RFC 3501) a unicode."""
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


CONFIG_FILE = Path(__file__).parent.parent / "config" / "cuentas.yaml"
DATA_DIR = Path(__file__).parent.parent / "data"

IMAP_TIMEOUT_SECONDS = 600


def load_account(domain, account_name):
    """Carga la configuracion de una cuenta desde cuentas.yaml."""
    if not CONFIG_FILE.exists():
        print(f"No se encontro {CONFIG_FILE}")
        print("Copia config/cuentas.example.yaml a config/cuentas.yaml y llena los datos.")
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    for dom in config.get("dominios", []):
        if dom["dominio"] == domain:
            for cuenta in dom.get("cuentas", []):
                if cuenta["nombre"] == account_name:
                    cuenta["imap_server"] = dom["imap_server"]
                    cuenta["imap_port"] = dom["imap_port"]
                    return cuenta

            available = [c["nombre"] for c in dom.get("cuentas", [])]
            print(f"Cuenta '{account_name}' no encontrada en dominio '{domain}'")
            print(f"Cuentas disponibles en {domain}: {', '.join(available)}")
            sys.exit(1)

    available_domains = [d["dominio"] for d in config.get("dominios", [])]
    print(f"Dominio '{domain}' no encontrado en {CONFIG_FILE}")
    print(f"Dominios disponibles: {', '.join(available_domains)}")
    sys.exit(1)


def read_password(password_file):
    """Lee la contrasena desde un archivo temporal.

    rstrip('\\r\\n') solo quita saltos de linea finales (los introduce
    'echo'), preservando espacios u otros caracteres legitimos.
    """
    path = Path(password_file)
    if not path.exists():
        print(f"No se encontro el archivo de contrasena: {password_file}")
        print(f"Crear el archivo con: echo 'tu-contrasena' > {password_file}")
        sys.exit(1)
    return path.read_text().rstrip("\r\n")


def parse_folder_response(folder_raw):
    """Devuelve (imap_name, display_name) o None si no se puede parsear.

    imap_name es el nombre tal cual el servidor lo espera (modified UTF-7)
    y se le pasa a mail.select(). display_name es la version unicode
    legible para humanos / disco.
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
    """Limpia un string para usarlo como nombre de archivo."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.strip('. ')
    return name[:max_len] if name else "sin_asunto"


def decode_subject(msg):
    """Extrae y decodifica el asunto del correo."""
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
        print("Uso: python scripts/backup.py <dominio> <nombre-cuenta>")
        print("Ejemplo: python scripts/backup.py <dominio> <cuenta>")
        sys.exit(1)

    domain = sys.argv[1]
    account_name = sys.argv[2]
    cuenta = load_account(domain, account_name)
    password = read_password(cuenta["password_file"])

    backup_dir = DATA_DIR / domain / account_name
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"Conectando a {cuenta['imap_server']}...")
    mail = imaplib.IMAP4_SSL(cuenta["imap_server"], cuenta["imap_port"], timeout=IMAP_TIMEOUT_SECONDS)
    mail.login(cuenta["email"], password)
    print(f"Autenticacion exitosa: {cuenta['email']}\n")

    status, folders = mail.list()
    if status != "OK":
        print("Error listando carpetas")
        sys.exit(1)

    total_descargados = 0

    uid_marker = backup_dir / ".uid-format"

    for folder_raw in folders:
        parsed = parse_folder_response(folder_raw)
        if not parsed:
            print(f"  ! Carpeta no parseable, saltada: {folder_raw!r}")
            continue
        imap_name, folder_name = parsed

        status, data = mail.select(f'"{imap_name}"', readonly=True)
        if status != "OK":
            print(f"  ! No se pudo abrir: {folder_name}")
            continue

        try:
            msg_count = int(data[0])
        except (TypeError, ValueError, IndexError) as e:
            print(f"  ! Respuesta inesperada de SELECT en {folder_name}: {e}")
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

        status, data = mail.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            print(f"  ! No se pudo listar UIDs en: {folder_name}")
            continue

        uids = data[0].split()
        for i, uid in enumerate(uids, 1):
            uid_str = uid.decode("ascii")
            try:
                status, msg_data = mail.uid("FETCH", uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                if not isinstance(raw_email, (bytes, bytearray)):
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

                if i % 50 == 0 or i == len(uids):
                    print(f"    Descargados {i}/{len(uids)}")

            except Exception as e:
                print(f"    ! Error en correo UID {uid_str}: {e}")
                continue

    try:
        mail.logout()
    except Exception:
        pass
    print(f"\nBackup completo: {total_descargados} correos descargados")
    print(f"Ubicacion: {backup_dir}")


if __name__ == "__main__":
    main()
