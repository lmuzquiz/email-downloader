# email-downloader

Herramientas en Python para respaldar buzones de correo completos via IMAP como
archivos `.eml` locales, organizados por dominio, cuenta y carpeta. Pensado para
migraciones de correo (respaldo del histórico antes de apagar el servidor viejo)
o para cualquier escenario donde necesites archivar cuentas IMAP de forma
robusta y reanudable.

## Qué incluye

Tres scripts que se complementan, de más simple a más completo:

| Script | Para qué sirve |
|--------|----------------|
| `scripts/backup.py` | Descarga inicial simple de una cuenta (sin lógica de skip). |
| `scripts/resume.py` | Descarga **idempotente y reanudable** de una cuenta. Salta los correos ya descargados, escribe atómicamente, reconecta si la red falla, emite una línea final `RESULT status=ok ...` parseable. En la práctica reemplaza a `backup.py` también para primera vez. |
| `scripts/orchestrator.py` | Corre N `resume.py` en paralelo (default 3) sobre una lista de cuentas. Maneja state persistente por corrida, reintentos con backoff, detección de drift en la config, recuperación de hard crash. Pensado para respaldar decenas de cuentas de forma **desatendida**. |

### Por qué `resume.py` es idempotente

- Identifica cada correo por su **UID IMAP** (`UID SEARCH ALL` + `UID FETCH`),
  que es estable entre sesiones a diferencia de los números de secuencia.
- Guarda como `<UID>_<asunto>.eml` dentro de la carpeta correspondiente.
- Antes de descargar, construye un set de UIDs ya presentes en disco y salta
  los que ya existen.
- Escribe cada `.eml` con `tmp + os.replace()`: si matas el proceso a medio
  write, no queda archivo truncado (queda un `.eml.tmp` huérfano que el
  próximo arranque limpia).
- Detecta "Broken pipe" / "EOF" en fetch individual y reconecta IMAP sin
  perder el progreso de la carpeta.

### Por qué el orquestador es robusto

- **Paralelismo por cuenta**, no por correo. Cada worker lanza `resume.py`
  como subproceso independiente.
- **State persistente** en `data/.orchestrator-runs/<run_id>/`:
  - `manifest.json`: snapshot de inputs de la corrida (inmutable salvo
    `--accept-drift`, ver más abajo).
  - `state.json`: estado vivo de cada cuenta (pending/running/completed/
    failed/dropped).
  - `logs/<dominio>__<cuenta>__<intento>.log`: stdout/stderr por intento.
- **Reintentos con backoff**: 60s, 5 min, 30 min. Después del tercer fallo
  marca la cuenta como `failed` sin bloquear a las demás.
- **Ctrl-C seguro**: termina los workers con SIGTERM, regresa las cuentas
  en `running` a `pending`, guarda state. Retomas con `--resume <run_id>`.
- **Recuperación de hard crash**: si el orquestador muere con `kill -9`,
  OOM o corte de luz, al hacer `--resume` detecta las cuentas huérfanas en
  `running` y las regresa a `pending` automáticamente (sin contar el crash
  como reintento).
- **Detección de drift**: si `cuentas.yaml` cambió desde que arrancó la
  corrida (cuentas removidas, password rotado, path cambiado, servidor
  distinto), imprime un diff y exige decidir entre `--accept-drift` (usar
  valores actuales) o revertir el archivo.

## Requisitos

- macOS, Linux o Windows
- [`uv`](https://github.com/astral-sh/uv) (gestor Python moderno). Los scripts
  usan headers PEP 723 para declarar dependencias inline, así que `uv` resuelve
  Python ≥3.12 y PyYAML automáticamente en un venv aislado por script. **No
  necesitas `pip install` nada.**

Instalación de `uv`:

```bash
# macOS
brew install uv

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# o:  winget install --id=astral-sh.uv -e
```

### Notas para Windows

Los scripts funcionan en Windows con tres ajustes menores respecto a la
documentación de abajo:

- **Invocación**: el shebang `#!/usr/bin/env -S uv run --quiet` no aplica en
  Windows. Usa `uv run scripts/foo.py ...` en lugar de `./scripts/foo.py ...`.
- **Paths de password files**: en lugar de `/tmp/mail-pass-x`, usa algo como
  `%TEMP%\mail-pass-x` o un path absoluto Windows. El path es solo un string
  en `cuentas.yaml`, el código no lo asume hardcoded.
- **Symlink `latest` del orquestador**: `os.symlink()` en Windows requiere
  privilegios de admin o "developer mode" activado. Sin eso, verás un warning
  `! No se pudo actualizar symlink 'latest'` y el orquestador seguirá
  funcionando normal — solo perdés el atajo `--resume latest` y tenés que
  pasar el `<run_id>` explícito (`--resume 2026-04-19T1030-...`).

## Configuración

1. Clonar el repo y copiar el template:

    ```bash
    git clone https://github.com/lmuzquiz/email-downloader.git
    cd email-downloader
    cp config/cuentas.example.yaml config/cuentas.yaml
    ```

2. Editar `config/cuentas.yaml` con los dominios y cuentas reales (estructura
   documentada dentro del template).

3. Crear los archivos de password referenciados por `password_file` en cada
   cuenta. Patrón típico:

    ```bash
    echo 'la-password-aqui' > /tmp/mail-pass-<algo>
    chmod 600 /tmp/mail-pass-<algo>
    ```

   `/tmp/` es ephemeral (se borra al reiniciar la máquina) — si te importa
   que sobreviva, usa otro path. Varias cuentas pueden compartir el mismo
   `password_file` si tienen la misma password.

## Uso

### Modo manual (una cuenta a la vez)

Para casos puntuales o debugging de una cuenta específica:

```bash
# Descarga reanudable (recomendada — funciona para primera vez también)
./scripts/resume.py <DOMINIO> <NOMBRE-CUENTA>

# Descarga simple (sin skip logic; rara vez necesaria)
./scripts/backup.py <DOMINIO> <NOMBRE-CUENTA>
```

`<DOMINIO>` y `<NOMBRE-CUENTA>` deben coincidir con una entrada del
`cuentas.yaml`. La salida termina con una línea como:

```
RESULT status=ok carpetas_total=7 carpetas_saltadas=0 correos_nuevos=151 correos_fallidos=0 reconexiones_fallidas=0
```

Exit code: `0` si todo limpio, `1` si hubo cualquier fallo parcial.

### Modo orquestado (muchas cuentas, desatendido)

```bash
# Nueva corrida contra uno o varios dominios
./scripts/orchestrator.py --dominios <DOMINIO1>,<DOMINIO2>,<DOMINIO3>

# Reanudar una corrida interrumpida
./scripts/orchestrator.py --resume <run_id>
./scripts/orchestrator.py --resume latest

# Listar corridas previas y su estado
./scripts/orchestrator.py --list-runs

# Ver drift del manifest sin reanudar
./scripts/orchestrator.py --resume <run_id> --diff-manifest

# Reanudar aceptando cambios en cuentas.yaml desde el manifest original
./scripts/orchestrator.py --resume <run_id> --accept-drift
```

Flags útiles:

- `--workers <N>`: default `3`. No subir sin probar con el servidor IMAP
  destino — puede rate-limitar la IP o cortar conexiones.
- `--max-reintentos <N>`: default `3`. Después de eso la cuenta queda
  `failed`.

### Cuándo usar cuál

- **1 cuenta:** modo manual.
- **3+ cuentas, o quieres dejarlo corriendo y olvidarte:** orquestador.
- **Migración masiva (decenas de buzones, horas de descarga):** orquestador,
  con passwords compartidas temporalmente entre cuentas del mismo dominio
  (todas apuntando al mismo `password_file`) para no tener que rotar cada una
  por separado.

## Organización de datos

```
data/
├── <dominio>/
│   └── <cuenta>/
│       ├── .uid-format            # marcador de que el directorio usa UIDs
│       ├── INBOX/
│       │   ├── 12345_asunto.eml   # prefijo = UID IMAP
│       │   └── ...
│       ├── INBOX.Sent/
│       └── ...
└── .orchestrator-runs/            # solo si usas el orquestador
    ├── latest -> <run_id>         # symlink al más reciente
    └── <run_id>/
        ├── manifest.json
        ├── state.json
        └── logs/
```

Todo dentro de `data/` está gitignored.

## Troubleshooting

### "Falta PyYAML" al ejecutar un script
Invocaste con `python3` directamente, saltándote el shebang de `uv`. Usa
`./scripts/foo.py` (respeta el shebang) o `uv run scripts/foo.py`.

### El worker falla con auth error
El `password_file` no existe, está vacío, o la password es incorrecta. El
orquestador reintenta 3 veces con backoff (60s/5min/30min) — si sigue
fallando, revisa el archivo y relanza con `--resume`.

### Nombres de carpeta con acentos / caracteres no-ASCII no se abren
Los nombres de carpeta en IMAP usan **modified UTF-7** (RFC 3501), no UTF-8.
Los scripts ya implementan el codec; si ves un error, reporta el output
exacto que da la consola.

### La corrida se interrumpió (Ctrl-C, kill, corte de luz)
Todo el estado está persistido en `data/.orchestrator-runs/<run_id>/`.
Relanza con `./scripts/orchestrator.py --resume <run_id>` (o `--resume
latest`). El orquestador recupera cuentas huérfanas en `running` y retoma
sin contar el crash como reintento.

### Hay archivos legacy con nombres tipo `00001_*.eml`
Son de una versión anterior que usaba números de secuencia IMAP. El código
actual usa UIDs y escribe con nombres `<UID>_<asunto>.eml`. Si tienes data
legacy y quieres reanudar limpio sobre ella, renombra la carpeta de la
cuenta antes (`mv data/<dom>/<cuenta> data/<dom>/<cuenta>.legacy-seq`) y
deja que el script haga una descarga nueva desde cero con UIDs.

## Licencia

MIT — ver `LICENSE`.
