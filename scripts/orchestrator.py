#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""
Orquestador de respaldos paralelos.

Mantiene un pool de N workers (default 3) que ejecutan resume.py contra una
queue de cuentas. Estado persistente por corrida en data/.orchestrator-runs/.

Uso:
    # Nueva corrida
    python scripts/orchestrator.py --dominios <dominio1>,<dominio2>,<dominio3>

    # Reanudar
    python scripts/orchestrator.py --resume <run_id>
    python scripts/orchestrator.py --resume latest

    # Inspeccionar
    python scripts/orchestrator.py --list-runs
    python scripts/orchestrator.py --resume <run_id> --diff-manifest
    python scripts/orchestrator.py --resume <run_id> --accept-drift
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Falta PyYAML. Ejecuta este script con uv: ./scripts/orchestrator.py o uv run scripts/orchestrator.py")


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config" / "cuentas.yaml"
DATA_DIR = REPO_ROOT / "data"
RUNS_DIR = DATA_DIR / ".orchestrator-runs"
RESUME_SCRIPT = REPO_ROOT / "scripts" / "resume.py"

BACKOFFS = [60, 300, 1800]
DASHBOARD_INTERVAL = 30
WORKER_POLL_INTERVAL = 2


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def hash_password_file(path):
    """Hash del contenido normalizado igual que como lo lee resume.py.

    rstrip('\\r\\n') aplica la misma normalizacion que read_password() en
    backup.py/resume.py, asi que un archivo con o sin newline final
    produce el mismo hash mientras la password efectiva sea identica.
    """
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text().rstrip("\r\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SLUG_MAX_LEN = 100


def slugify_dominios(dominios):
    """Slug compacto y filesystem-safe de la lista de dominios.

    Si el resultado supera SLUG_MAX_LEN, sustituye por '<N>dominios-<hash>'
    para evitar OSError al crear el run_dir (limite tipico ~255 chars en APFS/ext4
    pensando en que el run_id incluye ademas el timestamp).
    """
    parts = sorted(dominios)
    slug = "+".join(d.replace(".", "-") for d in parts)
    if len(slug) > SLUG_MAX_LEN:
        h = hashlib.sha256("+".join(parts).encode("utf-8")).hexdigest()[:8]
        slug = f"{len(parts)}dominios-{h}"
    return slug


def make_run_id(dominios):
    ts = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    return f"{ts}-{slugify_dominios(dominios)}"


def load_yaml():
    if not CONFIG_FILE.exists():
        sys.exit(f"No se encontro {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


REQUIRED_DOMAIN_FIELDS = ("dominio", "imap_server", "imap_port", "cuentas")
REQUIRED_CUENTA_FIELDS = ("nombre", "email", "password_file")


def build_manifest_from_yaml(dominios):
    config = load_yaml()
    if not isinstance(config, dict) or "dominios" not in config:
        sys.exit("cuentas.yaml: estructura invalida; falta el nodo raiz 'dominios'")

    manifest = {}
    for dom_idx, dom in enumerate(config.get("dominios", []) or []):
        if not isinstance(dom, dict):
            sys.exit(f"cuentas.yaml: dominios[{dom_idx}] no es un mapa")
        for field in REQUIRED_DOMAIN_FIELDS:
            if field not in dom:
                sys.exit(f"cuentas.yaml: dominios[{dom_idx}] falta campo requerido '{field}'")
        if dom["dominio"] not in dominios:
            continue
        for cuenta_idx, cuenta in enumerate(dom.get("cuentas") or []):
            if not isinstance(cuenta, dict):
                sys.exit(f"cuentas.yaml: {dom['dominio']}.cuentas[{cuenta_idx}] no es un mapa")
            if cuenta.get("omitir"):
                continue
            for field in REQUIRED_CUENTA_FIELDS:
                if field not in cuenta:
                    sys.exit(
                        f"cuentas.yaml: {dom['dominio']}.cuentas[{cuenta_idx}] "
                        f"falta campo requerido '{field}'"
                    )
            key = f"{dom['dominio']}/{cuenta['nombre']}"
            manifest[key] = {
                "dominio": dom["dominio"],
                "cuenta": cuenta["nombre"],
                "email": cuenta["email"],
                "password_file": cuenta["password_file"],
                "password_sha256": hash_password_file(cuenta["password_file"]),
                "imap_server": dom["imap_server"],
                "imap_port": dom["imap_port"],
            }
    return manifest


def diff_manifest(manifest, dominios):
    actual = build_manifest_from_yaml(dominios)
    drifts = []

    for key, m in manifest.items():
        if key not in actual:
            drifts.append({"key": key, "type": "removed", "manifest": m, "actual": None})
            continue
        a = actual[key]
        if m["password_file"] != a["password_file"]:
            drifts.append({"key": key, "type": "path_changed", "manifest": m, "actual": a})
        elif m["password_sha256"] != a["password_sha256"]:
            drifts.append({"key": key, "type": "password_content", "manifest": m, "actual": a})
        if m["imap_server"] != a["imap_server"] or m["imap_port"] != a["imap_port"]:
            drifts.append({"key": key, "type": "imap_changed", "manifest": m, "actual": a})

    for key, a in actual.items():
        if key not in manifest:
            drifts.append({"key": key, "type": "added", "manifest": None, "actual": a})

    return drifts


def print_drift(drifts):
    for d in drifts:
        if d["type"] == "removed":
            print(f"  [REMOVED] {d['key']}")
            print(f"      estaba en el manifest, ya no existe en cuentas.yaml")
        elif d["type"] == "added":
            print(f"  [ADDED, sera ignorada] {d['key']}")
            print(f"      no estaba en el manifest. Para incluirla, lanza un run nuevo con --dominios.")
        elif d["type"] == "path_changed":
            print(f"  [PATH CHANGED] {d['key']}")
            print(f"      manifest:  {d['manifest']['password_file']}")
            print(f"      actual:    {d['actual']['password_file']}")
        elif d["type"] == "password_content":
            print(f"  [PASSWORD CONTENT] {d['key']}")
            print(f"      el contenido de {d['manifest']['password_file']} cambio desde el manifest")
            print(f"      (probablemente rotacion de password)")
        elif d["type"] == "imap_changed":
            print(f"  [IMAP CHANGED] {d['key']}")
            print(f"      manifest:  {d['manifest']['imap_server']}:{d['manifest']['imap_port']}")
            print(f"      actual:    {d['actual']['imap_server']}:{d['actual']['imap_port']}")


def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def update_latest_symlink(run_id):
    """Actualiza el symlink data/.orchestrator-runs/latest a apuntar al run_id.

    Atomico: crea symlink temporal y hace os.replace(). Si el proceso muere
    a media operacion, el symlink anterior queda intacto en lugar de borrado
    sin reemplazo.
    """
    latest = RUNS_DIR / "latest"
    tmp_link = RUNS_DIR / ".latest.tmp"
    try:
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        tmp_link.symlink_to(run_id)
        os.replace(tmp_link, latest)
    except OSError as e:
        print(f"  ! No se pudo actualizar symlink 'latest': {e}")
        try:
            if tmp_link.is_symlink():
                tmp_link.unlink()
        except OSError:
            pass


_PATH_UNSAFE_RE = re.compile(r'[/\\<>:"|?*\x00-\x1f]')


def sanitize_for_filename(s):
    """Reemplaza caracteres invalidos en filename por _ (cross-platform)."""
    return _PATH_UNSAFE_RE.sub("_", s)


def create_run(dominios, workers, max_reintentos):
    run_id = make_run_id(dominios)
    run_dir = RUNS_DIR / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        sys.exit(
            f"Run ya existe: {run_dir}.\n"
            "Espera 1 segundo y reintenta, o usa --resume si quieres continuar el existente."
        )
    (run_dir / "logs").mkdir(exist_ok=True)

    manifest = build_manifest_from_yaml(dominios)
    if not manifest:
        sys.exit(f"No hay cuentas en cuentas.yaml para los dominios: {dominios}")

    manifest_data = {
        "run_id": run_id,
        "created_at": now_iso(),
        "dominios": dominios,
        "cuentas": manifest,
    }
    atomic_write_json(run_dir / "manifest.json", manifest_data)

    state = {
        "run_id": run_id,
        "started_at": now_iso(),
        "ended_at": None,
        "workers": workers,
        "max_reintentos": max_reintentos,
        "cuentas": {
            key: {
                "status": "pending",
                "reintentos": 0,
                "ultimo_error": None,
                "ultimo_result_line": None,
                "started_at": None,
                "completed_at": None,
                "correos_nuevos": None,
            }
            for key in manifest
        },
    }
    atomic_write_json(run_dir / "state.json", state)

    update_latest_symlink(run_id)

    return run_id, run_dir, manifest, state


def resolve_run_id(run_id_arg):
    if run_id_arg == "latest":
        latest = RUNS_DIR / "latest"
        if not latest.exists():
            sys.exit("No hay corridas previas (no existe data/.orchestrator-runs/latest)")
        return latest.resolve().name
    return run_id_arg


def load_run(run_id):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        sys.exit(f"Run no encontrado: {run_id}")
    manifest_data = json.loads((run_dir / "manifest.json").read_text())
    state = json.loads((run_dir / "state.json").read_text())
    return run_dir, manifest_data, state


def read_result_line(log_path):
    if not log_path.exists():
        return None
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 4096)
            f.seek(-chunk, 2)
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if line.startswith("RESULT "):
                return line
    except Exception:
        pass
    return None


def parse_correos_nuevos(result_line):
    m = re.search(r"correos_nuevos=(\d+)", result_line)
    return int(m.group(1)) if m else 0


class Pool:
    def __init__(self, run_dir, manifest, state, workers, max_reintentos):
        self.run_dir = run_dir
        self.manifest = manifest
        self.state = state
        self.max_workers = workers
        self.max_reintentos = max_reintentos
        self.lock = threading.Lock()
        self.queue = deque()
        self.scheduled = {}
        self.running = {}
        self.shutdown = threading.Event()
        self.workers = []

    def state_path(self):
        return self.run_dir / "state.json"

    def save_state(self):
        atomic_write_json(self.state_path(), self.state)

    def enqueue_pending(self):
        for key, info in self.state["cuentas"].items():
            if info["status"] == "pending":
                self.queue.append(key)

    def reset_running_to_pending(self):
        with self.lock:
            for key, info in self.state["cuentas"].items():
                if info["status"] == "running":
                    info["status"] = "pending"
                    info["started_at"] = None
            self.save_state()

    def claim_next(self):
        with self.lock:
            now = time.time()
            ready = [k for k in self.queue if self.scheduled.get(k, 0) <= now]
            if not ready:
                return None
            key = ready[0]
            self.queue.remove(key)
            self.scheduled.pop(key, None)
            self.state["cuentas"][key]["status"] = "running"
            self.state["cuentas"][key]["started_at"] = now_iso()
            self.save_state()
            return key

    def reschedule(self, key, error_msg, result_line=None):
        with self.lock:
            info = self.state["cuentas"][key]
            info["reintentos"] += 1
            info["ultimo_error"] = error_msg
            if result_line:
                info["ultimo_result_line"] = result_line
            if info["reintentos"] > self.max_reintentos:
                info["status"] = "failed"
                self.save_state()
                return
            backoff_idx = min(info["reintentos"] - 1, len(BACKOFFS) - 1)
            wait = BACKOFFS[backoff_idx]
            info["status"] = "pending"
            self.scheduled[key] = time.time() + wait
            self.queue.append(key)
            self.save_state()

    def complete(self, key, correos_nuevos, result_line):
        with self.lock:
            info = self.state["cuentas"][key]
            info["status"] = "completed"
            info["completed_at"] = now_iso()
            info["correos_nuevos"] = correos_nuevos
            info["ultimo_result_line"] = result_line
            self.save_state()

    def run_one(self, key):
        info = self.manifest.get(key)
        if info is None:
            self.reschedule(key, "no encontrada en manifest efectivo")
            return
        intento = self.state["cuentas"][key]["reintentos"] + 1
        log_name = (
            f"{sanitize_for_filename(info['dominio'])}__"
            f"{sanitize_for_filename(info['cuenta'])}__{intento}.log"
        )
        log_path = self.run_dir / "logs" / log_name

        try:
            with open(log_path, "w") as logf:
                # Invocacion explicita via 'uv run' en vez de [str(RESUME_SCRIPT), ...]
                # para no depender del bit de ejecucion (chmod +x) del script. Si el
                # repo se clonara con permisos sin +x (algunos checkouts lo pierden),
                # la version anterior fallaba con PermissionError.
                proc = subprocess.Popen(
                    ["uv", "run", "--quiet", str(RESUME_SCRIPT),
                     info["dominio"], info["cuenta"]],
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                )
                with self.lock:
                    self.running[key] = proc
                rc = proc.wait()
            with self.lock:
                self.running.pop(key, None)
        except Exception as e:
            with self.lock:
                self.running.pop(key, None)
            self.reschedule(key, f"orchestrator error: {e}")
            return

        if self.shutdown.is_set():
            return

        result_line = read_result_line(log_path)

        if rc == 0 and result_line and result_line.startswith("RESULT status=ok"):
            correos_nuevos = parse_correos_nuevos(result_line)
            self.complete(key, correos_nuevos, result_line)
        else:
            error = f"exit={rc}"
            if result_line:
                error += f" | {result_line}"
            else:
                error += " | (sin RESULT line)"
            self.reschedule(key, error, result_line)

    def worker_loop(self):
        while not self.shutdown.is_set():
            key = self.claim_next()
            if key is None:
                time.sleep(WORKER_POLL_INTERVAL)
                with self.lock:
                    if not self.queue and not self.running:
                        return
                continue
            self.run_one(key)

    def terminate_running(self):
        with self.lock:
            for key, proc in list(self.running.items()):
                try:
                    proc.terminate()
                except Exception:
                    pass

    def start(self):
        for _ in range(self.max_workers):
            t = threading.Thread(target=self.worker_loop, daemon=True)
            t.start()
            self.workers.append(t)

    def wait(self):
        for t in self.workers:
            t.join()


def render_dashboard(state):
    counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "dropped": 0}
    for info in state["cuentas"].values():
        counts[info["status"]] = counts.get(info["status"], 0) + 1
    total = sum(counts.values())
    return (f"[{now_iso()}] run={state['run_id']} | total={total} "
            f"pending={counts['pending']} running={counts['running']} "
            f"completed={counts['completed']} failed={counts['failed']} "
            f"dropped={counts['dropped']}")


def dashboard_loop(pool):
    while not pool.shutdown.is_set():
        with pool.lock:
            line = render_dashboard(pool.state)
            running_keys = list(pool.running.keys())
        print(line)
        if running_keys:
            print(f"  corriendo: {', '.join(running_keys)}")
        for _ in range(DASHBOARD_INTERVAL):
            if pool.shutdown.is_set():
                return
            time.sleep(1)


def print_final_report(state, run_dir):
    print("\n" + "=" * 70)
    print(f"Run finalizado: {state['run_id']}")
    print(f"Inicio: {state['started_at']}")
    print(f"Fin:    {state.get('ended_at') or now_iso()}")
    print(f"Logs:   {run_dir / 'logs'}")
    print()

    by_dominio = {}
    for key, info in state["cuentas"].items():
        dom, cuenta = key.split("/", 1)
        by_dominio.setdefault(dom, []).append((cuenta, info))

    for dom, items in sorted(by_dominio.items()):
        print(f"## {dom}\n")
        print(f"| Cuenta | Estado | Correos nuevos | Reintentos | Notas |")
        print(f"|--------|--------|----------------|------------|-------|")
        for cuenta, info in sorted(items):
            estado = info["status"]
            correos = info["correos_nuevos"] if info["correos_nuevos"] is not None else "—"
            reintentos = info["reintentos"]
            nota = ""
            if estado == "failed":
                nota = (info["ultimo_error"] or "")[:60]
            elif estado == "dropped":
                nota = "removida de cuentas.yaml via --accept-drift"
            print(f"| {cuenta} | {estado} | {correos} | {reintentos} | {nota} |")
        print()


def cmd_list_runs():
    if not RUNS_DIR.exists():
        print("No hay corridas previas.")
        return
    entries = []
    for p in sorted(RUNS_DIR.iterdir()):
        if p.name == "latest" or not p.is_dir():
            continue
        state_path = p / "state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            continue
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "dropped": 0}
        for info in state["cuentas"].values():
            counts[info["status"]] = counts.get(info["status"], 0) + 1
        entries.append((p.name, state.get("started_at"), state.get("ended_at"), counts))

    if not entries:
        print("No hay corridas con state.json.")
        return

    for name, start, end, counts in entries:
        end_str = end or "(en curso o interrumpida)"
        total = sum(counts.values())
        print(f"{name}")
        print(f"  inicio: {start}")
        print(f"  fin:    {end_str}")
        print(f"  total={total} pending={counts['pending']} running={counts['running']} "
              f"completed={counts['completed']} failed={counts['failed']} "
              f"dropped={counts['dropped']}")
        print()


def cmd_diff_manifest(run_id):
    run_id = resolve_run_id(run_id)
    run_dir, manifest_data, state = load_run(run_id)
    drifts = diff_manifest(manifest_data["cuentas"], manifest_data["dominios"])
    if not drifts:
        print(f"Sin drift entre el manifest de {run_id} y cuentas.yaml actual.")
        return
    print(f"Drift detectado en {run_id}:\n")
    print_drift(drifts)


def cmd_resume(run_id, accept_drift, workers_arg, max_reintentos_arg):
    run_id = resolve_run_id(run_id)
    run_dir, manifest_data, state = load_run(run_id)

    recovered = 0
    for key, info in state["cuentas"].items():
        if info["status"] == "running":
            info["status"] = "pending"
            info["started_at"] = None
            recovered += 1
    if recovered:
        atomic_write_json(run_dir / "state.json", state)
        print(f"Recuperadas {recovered} cuentas en estado 'running' (terminacion no limpia previa).")

    drifts = diff_manifest(manifest_data["cuentas"], manifest_data["dominios"])

    if drifts and not accept_drift:
        print(f"Run: {run_id}")
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "dropped": 0}
        for info in state["cuentas"].values():
            counts[info["status"]] = counts.get(info["status"], 0) + 1
        print(f"  pendientes: {counts['pending']} | corriendo: {counts['running']} | "
              f"completadas: {counts['completed']} | failed: {counts['failed']} | "
              f"dropped: {counts['dropped']}")
        print()
        print("DRIFT detectado entre el manifest y cuentas.yaml actual:\n")
        print_drift(drifts)
        print()
        print("Acciones posibles:")
        print()
        print("  a) Continuar usando los valores ACTUALES de cuentas.yaml:")
        print(f"       --resume {run_id} --accept-drift")
        print("     (las cuentas REMOVED se marcan como 'dropped' y se omiten;")
        print("      las cuentas con cambios usan los valores nuevos)")
        print()
        print("  b) Si el cambio en cuentas.yaml fue accidental, revertir el archivo")
        print("     a su estado original y volver a --resume sin ningun flag.")
        print()
        print("  c) Para solo inspeccionar sin actuar:")
        print(f"       --resume {run_id} --diff-manifest")
        sys.exit(2)

    if accept_drift:
        actual = build_manifest_from_yaml(manifest_data["dominios"])
        for d in drifts:
            if d["type"] == "removed":
                key = d["key"]
                if key in state["cuentas"] and state["cuentas"][key]["status"] in ("pending", "failed"):
                    state["cuentas"][key]["status"] = "dropped"
        effective_manifest = {
            k: v for k, v in actual.items()
            if k in state["cuentas"] and state["cuentas"][k]["status"] != "dropped"
        }
        atomic_write_json(run_dir / "state.json", state)

        # Reescribir manifest.json con el baseline aceptado para que la siguiente
        # --resume no vuelva a detectar el mismo drift y bloquear. Las cuentas
        # historicas marcadas como 'dropped' siguen visibles en state.json para
        # auditoria; el manifest refleja el set vigente del run.
        manifest_data["cuentas"] = actual
        manifest_data["last_drift_accepted_at"] = now_iso()
        atomic_write_json(run_dir / "manifest.json", manifest_data)
    else:
        effective_manifest = manifest_data["cuentas"]

    workers = workers_arg if workers_arg is not None else state["workers"]
    max_reintentos = max_reintentos_arg if max_reintentos_arg is not None else state["max_reintentos"]

    persisted_change = False
    if workers_arg is not None and workers != state["workers"]:
        state["workers"] = workers
        persisted_change = True
    if max_reintentos_arg is not None and max_reintentos != state["max_reintentos"]:
        state["max_reintentos"] = max_reintentos
        persisted_change = True
    if persisted_change:
        atomic_write_json(run_dir / "state.json", state)

    update_latest_symlink(run_id)
    run_pool(run_dir, effective_manifest, state, workers, max_reintentos)


def cmd_new(dominios, workers, max_reintentos):
    run_id, run_dir, manifest, state = create_run(dominios, workers, max_reintentos)
    print(f"Run creado: {run_id}")
    print(f"  cuentas: {len(manifest)}")
    print(f"  workers: {workers}")
    print()
    run_pool(run_dir, manifest, state, workers, max_reintentos)


def run_pool(run_dir, manifest, state, workers, max_reintentos):
    pool = Pool(run_dir, manifest, state, workers, max_reintentos)
    pool.enqueue_pending()

    if not pool.queue:
        print("No hay cuentas pendientes en este run. Nada que hacer.")
        state["ended_at"] = now_iso()
        atomic_write_json(run_dir / "state.json", state)
        print_final_report(state, run_dir)
        return

    def handle_sigint(sig, frame):
        print("\nCtrl-C recibido. Terminando workers...")
        pool.shutdown.set()
        pool.terminate_running()

    signal.signal(signal.SIGINT, handle_sigint)

    pool.start()
    dash_thread = threading.Thread(target=dashboard_loop, args=(pool,), daemon=True)
    dash_thread.start()

    try:
        pool.wait()
    finally:
        pool.shutdown.set()
        pool.reset_running_to_pending()
        state["ended_at"] = now_iso()
        atomic_write_json(run_dir / "state.json", state)

    print_final_report(state, run_dir)


def main():
    parser = argparse.ArgumentParser(description="Orquestador de respaldos paralelos.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dominios", help="Lista de dominios separados por coma (corrida nueva)")
    group.add_argument("--resume", help="run_id a reanudar (o 'latest')")
    group.add_argument("--list-runs", action="store_true", help="Lista las corridas previas y su estado")
    parser.add_argument("--workers", type=int, default=None,
                        help="Numero de workers paralelos (default 3 en run nuevo; "
                             "default = valor del state en --resume)")
    parser.add_argument("--max-reintentos", type=int, default=None,
                        help="Max reintentos por cuenta (default 3 en run nuevo; "
                             "default = valor del state en --resume)")
    parser.add_argument("--accept-drift", action="store_true",
                        help="Aceptar drift y usar valores actuales de cuentas.yaml (requiere --resume)")
    parser.add_argument("--diff-manifest", action="store_true",
                        help="Solo mostrar diff de drift, no lanzar workers (requiere --resume)")
    args = parser.parse_args()

    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    if args.list_runs:
        cmd_list_runs()
        return

    if args.diff_manifest:
        if not args.resume:
            sys.exit("--diff-manifest requiere --resume")
        cmd_diff_manifest(args.resume)
        return

    if args.resume:
        cmd_resume(args.resume, args.accept_drift, args.workers, args.max_reintentos)
        return

    if args.dominios:
        if args.accept_drift:
            sys.exit("--accept-drift requiere --resume")
        dominios = [d.strip() for d in args.dominios.split(",") if d.strip()]
        if not dominios:
            sys.exit("--dominios vacio")
        workers = args.workers if args.workers is not None else 3
        max_reintentos = args.max_reintentos if args.max_reintentos is not None else 3
        cmd_new(dominios, workers, max_reintentos)
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
