"""One-command test runner for the scenario templates.

Usage (from the repository root):

    python scripts/test.py                                  # static + unit for every template
    python scripts/test.py --template customer-returns-queue --level local
    python scripts/test.py --template customer-returns-queue --level deploy [--down]

Levels are cumulative:

    static  Bicep compiles; hosted skill files and tools load with the runtime.
    unit    pytest in <template>/tests.
    local   func start against Azurite; every hosted skill registers as a function.
    deploy  azd up, then <template>/scripts/demo.py e2e against Azure. Creates billable resources.

Each template gets its own virtual environment in <template>/.venv.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEVELS = ["static", "unit", "local", "deploy"]
IS_WINDOWS = os.name == "nt"

STATIC_CHECK = r"""
import json, os, sys
from pathlib import Path
src = Path(sys.argv[1])
sample = src / "local.settings.json.sample"
if sample.exists():
    for key, value in json.loads(sample.read_text(encoding="utf-8"))["Values"].items():
        os.environ.setdefault(key, value)
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.discovery.tools import discover_project_tools
load_global_config(src)
specs = load_agent_specs(src, strict=True)
if not specs:
    sys.exit("no hosted skill (*.agent.md) found")
tools = discover_project_tools(src)
if tools.failed_loads:
    sys.exit(f"tool load failures: {tools.failed_loads}")
names = [Path(s.source_file).name.split(".")[0] for s in specs]
print(json.dumps({"functions": names, "tools": sorted(t.name for t in tools.user_tools)}))
"""


class StepFailed(Exception):
    pass


def log(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, capture: bool = False) -> str:
    print("$ " + " ".join(a if "\n" not in a else "<script>" for a in cmd), flush=True)
    result = subprocess.run(
        cmd, cwd=cwd, env=env, text=True, capture_output=capture, shell=IS_WINDOWS and cmd[0] in {"az", "azd", "func"}
    )
    if result.returncode != 0:
        if capture:
            print(result.stdout, result.stderr, sep="\n")
        raise StepFailed(f"{' '.join(cmd)} exited with {result.returncode}")
    return result.stdout if capture else ""


def discover_templates() -> list[Path]:
    return sorted(p.parent for p in ROOT.glob("*/azure.yaml"))


def venv_python(template: Path) -> Path:
    venv = template / ".venv"
    python = venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    if not python.exists():
        log(f"creating {venv}")
        run([sys.executable, "-m", "venv", str(venv)], cwd=template)
    marker = venv / ".requirements-installed"
    requirements = template / "requirements-dev.txt"
    sources = [requirements, template / "src" / "requirements.txt"]
    if not marker.exists() or any(p.stat().st_mtime > marker.stat().st_mtime for p in sources if p.exists()):
        log("installing requirements")
        run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--pre",
             "-r", str(requirements)], cwd=template)
        marker.touch()
    return python


def static(template: Path, python: Path) -> list[str]:
    log("static: bicep build")
    run(["az", "bicep", "build", "--file", "infra/main.bicep", "--stdout"], cwd=template, capture=True)
    log("static: hosted skills and tools")
    out = run([str(python), "-c", STATIC_CHECK, str(template / "src")], cwd=template / "src", capture=True)
    info = json.loads(out.strip().splitlines()[-1])
    print(f"functions: {info['functions']}\ntools: {info['tools']}")
    return info["functions"]


def unit(template: Path, python: Path) -> None:
    if not (template / "tests").is_dir():
        print("no tests folder; skipping")
        return
    log("unit: pytest")
    run([str(python), "-m", "pytest", "-q", "tests"], cwd=template)


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        os.killpg(proc.pid, 15)
    proc.wait(timeout=30)


def local(template: Path, python: Path, functions: list[str], port: int = 7071, timeout: int = 180) -> None:
    log("local: func start")
    if shutil.which("func") is None:
        raise StepFailed("Azure Functions Core Tools (func) is not installed")
    if not (_port_open(10000) and _port_open(10001)):
        raise StepFailed("Azurite is not running on ports 10000-10002 (start it with `azurite` or Docker)")
    src = template / "src"
    settings = src / "local.settings.json"
    if not settings.exists():
        shutil.copy(src / "local.settings.json.sample", settings)
        print(f"created {settings.relative_to(ROOT)} from the sample")

    env = dict(os.environ)
    env["PATH"] = f"{python.parent}{os.pathsep}{env['PATH']}"
    env["VIRTUAL_ENV"] = str(python.parent.parent)
    log_file = template / ".func-start.log"
    with log_file.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            ["func", "start", "--port", str(port)], cwd=src, env=env, stdout=out, stderr=subprocess.STDOUT,
            shell=IS_WINDOWS, start_new_session=not IS_WINDOWS,
        )
        try:
            deadline = time.monotonic() + timeout
            found: set[str] = set()
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise StepFailed(f"func exited early; see {log_file}")
                try:
                    with urllib.request.urlopen(f"http://localhost:{port}/admin/functions", timeout=5) as r:
                        found = {f["name"] for f in json.load(r)}
                except OSError:
                    found = set()
                if found and set(functions) <= found:
                    print(f"registered functions: {sorted(found)}")
                    return
                time.sleep(3)
            raise StepFailed(f"functions {functions} not registered (found {sorted(found)}); see {log_file}")
        finally:
            _kill_tree(proc)


def deploy(template: Path, python: Path, down: bool) -> None:
    log("deploy: azd up")
    try:
        try:
            run(["azd", "up", "--no-prompt"], cwd=template)
        except StepFailed:
            # New role assignments can take a few minutes to reach the data plane, which makes
            # the first package upload fail with 403. Provisioning is done; retry the deploy step.
            print("azd up failed; retrying azd deploy in 60 seconds")
            time.sleep(60)
            run(["azd", "deploy", "--no-prompt"], cwd=template)
        log("deploy: demo e2e")
        run([str(python), "scripts/demo.py", "e2e"], cwd=template)
    finally:
        if down:
            log("deploy: azd down")
            run(["azd", "down", "--purge", "--force"], cwd=template)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", action="append", help="Template folder name. Repeatable. Default: all.")
    parser.add_argument("--level", choices=LEVELS, default="unit")
    parser.add_argument("--down", action="store_true", help="Run azd down after the deploy level.")
    args = parser.parse_args()

    templates = discover_templates()
    if args.template:
        by_name = {t.name: t for t in templates}
        unknown = [n for n in args.template if n not in by_name]
        if unknown:
            parser.error(f"unknown template(s) {unknown}; available: {sorted(by_name)}")
        templates = [by_name[n] for n in args.template]

    depth = LEVELS.index(args.level)
    failures = []
    for template in templates:
        print(f"\n######## {template.name} (level: {args.level})")
        try:
            python = venv_python(template)
            functions = static(template, python)
            if depth >= 1:
                unit(template, python)
            if depth >= 2:
                local(template, python, functions)
            if depth >= 3:
                deploy(template, python, args.down)
        except StepFailed as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            failures.append(template.name)

    print("\n" + ("all passed" if not failures else f"failed: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
