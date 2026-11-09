#!/opt/mcp-admin/venv/bin/python
"""
MCP d'administration (phase d'installation) pour le serveur GPU.
HTTP simple, pas de TLS : le secret est le segment de chemin /<TOKEN>/.
URL à donner à Claude : http://<IP>:<PORT>/<TOKEN>/mcp
"""
import os
import pathlib
import subprocess
import contextlib

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

TOKEN = os.environ["MCP_TOKEN"]
PORT = int(os.environ.get("MCP_PORT", "8765"))
MAX_OUT = 30_000  # caractères renvoyés max par sortie

mcp = FastMCP(
    "gpu-admin",
    stateless_http=True,
    json_response=True,
    # Derrière Apache + token dans l'URL : pas de contrôle Host/Origin
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


def _tail(s: str) -> str:
    return s if len(s) <= MAX_OUT else "…[tronqué]…\n" + s[-MAX_OUT:]


@mcp.tool()
def run_shell(command: str, timeout: int = 600, cwd: str = "/root") -> str:
    """Exécute une commande bash en root. Renvoie le code de sortie, stdout et stderr."""
    try:
        r = subprocess.run(
            command, shell=True, executable="/bin/bash", cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT après {timeout}s"
    return (
        f"exit={r.returncode}\n--- stdout ---\n{_tail(r.stdout)}"
        f"\n--- stderr ---\n{_tail(r.stderr)}"
    )


@mcp.tool()
def read_file(path: str, offset: int = 0, limit: int = 500) -> str:
    """Lit un fichier texte (lignes offset..offset+limit)."""
    p = pathlib.Path(path)
    if not p.is_file():
        return f"ERREUR: {path} introuvable"
    lines = p.read_text(errors="replace").splitlines()
    chunk = lines[offset:offset + limit]
    return f"{len(lines)} lignes au total\n" + "\n".join(
        f"{offset + i + 1}\t{l}" for i, l in enumerate(chunk)
    )


@mcp.tool()
def write_file(path: str, content: str, mode: str = "0644") -> str:
    """Écrit (ou remplace) un fichier texte, crée les dossiers parents."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    os.chmod(p, int(mode, 8))
    return f"OK {path} ({len(content)} octets)"


@mcp.tool()
def list_dir(path: str = "/") -> str:
    """Liste un dossier (type, taille, nom)."""
    p = pathlib.Path(path)
    if not p.is_dir():
        return f"ERREUR: {path} n'est pas un dossier"
    out = []
    for e in sorted(p.iterdir()):
        try:
            size = e.stat().st_size
        except OSError:
            size = -1
        out.append(f"{'d' if e.is_dir() else 'f'}\t{size}\t{e.name}")
    return "\n".join(out) or "(vide)"


@mcp.tool()
def gpu_status() -> str:
    """nvidia-smi : état du GPU et processus en cours."""
    return run_shell("nvidia-smi", timeout=30)


@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[Mount(f"/{TOKEN}", app=mcp.streamable_http_app())],
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")