#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  LAMCAP — Local Agentic Multi-Context Automation Protocol                  ║
║  A stateful, multi-agent CLI automation engine for Termux / local shells.  ║
║                                                                            ║
║  Architecture:                                                             ║
║    1. Context Aggregation Layer  — SQLite-backed persistent memory          ║
║    2. Multi-Agent Orchestration  — Planner → Validator → Executor          ║
║    3. Local LLM Inference Engine — Copilot-to-LAMCAP proxy bridge       ║
║    4. Model Trigger Mapping      — Student Token Multiplier display        ║
║    5. Execution Interface & UI   — Rich splash + prompt_toolkit REPL       ║
║                                                                            ║
║  Usage:                                                                    ║
║    python lamcap.py                                                        ║
║                                                                            ║
║  Environment Variables:                                                    ║
║    ANTHROPIC_BASE_URL  — Proxy endpoint  (default: http://localhost:4141)   ║
║    ANTHROPIC_API_KEY   — Bearer token    (default: "lamcap-local")         ║
║    ANTHROPIC_MODEL     — Model trigger   (default: "gpt-4.1")             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Third-party imports — fail fast with helpful messages
# ──────────────────────────────────────────────────────────────────────────────
try:
    import anthropic
except ImportError:
    sys.exit(
        "[LAMCAP] Missing dependency: anthropic\n"
        "  → pip install anthropic"
    )

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.columns import Columns
    from rich.markdown import Markdown
    from rich import box
except ImportError:
    sys.exit(
        "[LAMCAP] Missing dependency: rich\n"
        "  → pip install rich"
    )

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style as PTKStyle
except ImportError:
    sys.exit(
        "[LAMCAP] Missing dependency: prompt_toolkit\n"
        "  → pip install prompt_toolkit"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 0.  CONSTANTS & CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

VERSION = "0.1.0"

# Where the SQLite database lives (next to this script or in cwd)
DB_PATH = os.path.join(os.getcwd(), "lamcap.db")

# History file for prompt_toolkit (persists across sessions)
REPL_HISTORY_PATH = os.path.join(os.getcwd(), ".lamcap_history")

# ── Environment-driven proxy configuration ────────────────────────────────
ANTHROPIC_BASE_URL: str = os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:4141")
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "lamcap-local")
ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "gpt-4.1")

# ── Model Trigger → Student Token Multiplier mapping ──────────────────────
MODEL_MULTIPLIER_MAP: dict[str, float] = {
    "gpt-4.1":                0.0,
    "gpt-4o":                 0.0,
    "grok-code-fast-1":       0.25,
    "claude-haiku-4.5":       0.33,
    "gemini-3-flash-preview": 0.33,
    "gemini-3.1-pro-preview": 1.0,
}

# ── Destructive command patterns the Validator will hard-block ────────────
BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/\s*$"),   # rm -rf /
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+-[a-zA-Z]*f[a-zA-Z]*\s+/\s*$"),
    re.compile(r"\brm\s+-rf\s+/\b"),                           # rm -rf /anything from root
    re.compile(r"\brm\s+-rf\s+\*"),                            # rm -rf *
    re.compile(r"\brm\s+-rf\s+~"),                             # rm -rf ~
    re.compile(r"\bchmod\s+777\b"),                            # chmod 777
    re.compile(r"\bmkfs\b"),                                   # mkfs (format)
    re.compile(r"\bdd\s+.*of=/dev/"),                          # dd to device
    re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;"),             # fork bomb
    re.compile(r"\b>\s*/dev/sd[a-z]"),                         # overwrite disk
    re.compile(r"\bsudo\s+rm\s+-rf\s+/"),                     # sudo rm -rf /
]

# ── Rich console singleton ────────────────────────────────────────────────
console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# 1.  CONTEXT AGGREGATION LAYER — SQLite
# ══════════════════════════════════════════════════════════════════════════════

class ContextStore:
    """
    Persistent SQLite context store for LAMCAP.

    Tables:
      • user_history  — every prompt, planned tasks, command outputs
      • fs_snapshots  — periodic snapshots of the working-directory tree
    """

    DDL = textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS user_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            role        TEXT    NOT NULL CHECK(role IN ('user','planner','validator','executor','system')),
            prompt      TEXT,
            plan_json   TEXT,
            command     TEXT,
            stdout      TEXT,
            stderr      TEXT,
            exit_code   INTEGER,
            model       TEXT,
            multiplier  REAL
        );

        CREATE TABLE IF NOT EXISTS fs_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            cwd         TEXT    NOT NULL,
            tree_json   TEXT    NOT NULL
        );
    """)

    def __init__(self, db_path: str = DB_PATH) -> None:
        """Open (or create) the database and ensure the schema exists."""
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.DDL)
        self.conn.commit()

    # ── History helpers ───────────────────────────────────────────────────

    def log_user_prompt(self, prompt: str, model: str, multiplier: float) -> int:
        """Record a raw user prompt and return the row id."""
        cur = self.conn.execute(
            "INSERT INTO user_history (role, prompt, model, multiplier) VALUES (?, ?, ?, ?)",
            ("user", prompt, model, multiplier),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def log_plan(self, plan_json: str) -> int:
        """Record the Planner agent's structured output."""
        cur = self.conn.execute(
            "INSERT INTO user_history (role, plan_json) VALUES (?, ?)",
            ("planner", plan_json),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def log_execution(self, command: str, stdout: str, stderr: str, exit_code: int) -> int:
        """Record an executed command and its results."""
        cur = self.conn.execute(
            "INSERT INTO user_history (role, command, stdout, stderr, exit_code) VALUES (?, ?, ?, ?, ?)",
            ("executor", command, stdout, stderr, exit_code),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def recent_history(self, limit: int = 20) -> list[dict]:
        """Return the most recent N history rows as dicts."""
        rows = self.conn.execute(
            "SELECT * FROM user_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Filesystem snapshot helpers ───────────────────────────────────────

    @staticmethod
    def _walk_tree(root: str, max_depth: int = 3, max_files: int = 200) -> list[str]:
        """
        Walk the directory tree starting from *root* up to *max_depth* levels,
        returning a flat list of relative paths (capped at *max_files*).
        Ignores hidden directories (dot-prefixed) to keep context clean.
        """
        entries: list[str] = []
        root_path = Path(root)
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune hidden dirs in-place so os.walk skips them
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            depth = len(Path(dirpath).relative_to(root_path).parts)
            if depth > max_depth:
                dirnames.clear()
                continue
            for f in filenames:
                if f.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, f), root)
                entries.append(rel)
                if len(entries) >= max_files:
                    return entries
        return entries

    def snapshot_cwd(self) -> dict:
        """Capture and persist the current working directory tree."""
        cwd = os.getcwd()
        tree = self._walk_tree(cwd)
        tree_json = json.dumps(tree)
        self.conn.execute(
            "INSERT INTO fs_snapshots (cwd, tree_json) VALUES (?, ?)",
            (cwd, tree_json),
        )
        self.conn.commit()
        return {"cwd": cwd, "files": tree}

    def build_system_context(self) -> str:
        """
        Compile a system-prompt context block from the database.
        Includes: recent interaction history + latest cwd snapshot.
        """
        snap = self.snapshot_cwd()
        history = self.recent_history(limit=15)

        ctx_parts = [
            "## LAMCAP — Context Snapshot",
            f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
            f"Working directory: {snap['cwd']}",
            f"Files in tree ({len(snap['files'])} shown): {json.dumps(snap['files'][:60])}",
            "",
            "## Recent Interaction History",
        ]

        for row in history:
            if row.get("prompt"):
                ctx_parts.append(f"[user] {row['prompt']}")
            if row.get("plan_json"):
                ctx_parts.append(f"[planner] {row['plan_json'][:300]}")
            if row.get("command"):
                exit_code = row.get("exit_code", "?")
                ctx_parts.append(f"[executor] $ {row['command']}  (exit {exit_code})")
                if row.get("stdout"):
                    ctx_parts.append(f"  stdout: {row['stdout'][:200]}")
                if row.get("stderr"):
                    ctx_parts.append(f"  stderr: {row['stderr'][:200]}")

        return "\n".join(ctx_parts)

    def close(self) -> None:
        self.conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2.  LOCAL LLM INFERENCE ENGINE — The Proxy Bridge
# ══════════════════════════════════════════════════════════════════════════════

import socket
from urllib.parse import urlparse

def check_bridge_connection(url: str, timeout: float = 1.0) -> bool:
    """
    Check if the proxy bridge is reachable by attempting to open a socket
    on the host/port specified in the URL.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False

class InferenceEngine:
    """
    Anthropic-compatible client that routes ALL traffic through a local
    Copilot-to-LAMCAP proxy (default: localhost:4141).

    Environment variables control every tuneable:
      ANTHROPIC_BASE_URL  → proxy endpoint
      ANTHROPIC_API_KEY   → bearer token / passthrough
      ANTHROPIC_MODEL     → model trigger string
    """

    def __init__(
        self,
        base_url: str = ANTHROPIC_BASE_URL,
        api_key: str = ANTHROPIC_API_KEY,
        model: str = ANTHROPIC_MODEL,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.multiplier = MODEL_MULTIPLIER_MAP.get(model, 0.0)

        # Build the Anthropic client pointed at the local proxy
        self.client = anthropic.Anthropic(
            base_url=self.base_url,
            api_key=self.api_key,
        )

    def infer(self, system_prompt: str, user_message: str, max_tokens: int = 4096) -> str:
        """
        Send a single-turn inference request through the proxy bridge.

        Args:
            system_prompt: Compiled context from the SQLite layer.
            user_message:  The user's natural-language intent.
            max_tokens:    Response length cap.

        Returns:
            The assistant's text response.

        Raises:
            ConnectionError: If the proxy is unreachable.
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            # Extract text from content blocks
            text_blocks = [
                block.text for block in response.content if hasattr(block, "text")
            ]
            return "\n".join(text_blocks)

        except anthropic.APIConnectionError as exc:
            raise ConnectionError(
                f"[LAMCAP] Proxy bridge unreachable at {self.base_url}\n"
                f"  → Ensure your Copilot-to-LAMCAP tunnel is running.\n"
                f"  → Detail: {exc}"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ConnectionError(
                f"[LAMCAP] Proxy returned an error (HTTP {exc.status_code}).\n"
                f"  → {exc.message}"
            ) from exc


# ══════════════════════════════════════════════════════════════════════════════
# 3.  MULTI-AGENT ORCHESTRATION LAYER
# ══════════════════════════════════════════════════════════════════════════════

# ── 3a. Planner Agent ─────────────────────────────────────────────────────

class PlannerAgent:
    """
    Takes a raw user prompt plus SQLite context and asks the LLM to return
    a JSON-structured list of concrete terminal sub-tasks.

    Expected output schema from the LLM:
        {
          "tasks": [
            {"step": 1, "command": "...", "description": "..."},
            ...
          ]
        }
    """

    PLANNER_INSTRUCTIONS = textwrap.dedent("""\
        You are the LAMCAP Planner Agent. Your purpose is to decompose the
        user's intent into a list of concrete terminal commands that can be
        executed sequentially in a Linux / Termux shell.

        RULES:
        1. Return ONLY valid JSON — no markdown fences, no commentary.
        2. Use the exact schema:
           {"tasks": [{"step": <int>, "command": "<shell command>", "description": "<why>"}]}
        3. Each command must be a single, self-contained shell invocation.
        4. Prefer non-destructive, idempotent commands when possible.
        5. Use the file-system context and history provided to avoid redundant work.
    """)

    def __init__(self, engine: InferenceEngine, store: ContextStore) -> None:
        self.engine = engine
        self.store = store

    def plan(self, user_prompt: str) -> dict:
        """
        Generate a structured execution plan for *user_prompt*.

        Returns:
            A dict with a "tasks" key containing a list of step dicts.
        """
        system_ctx = self.store.build_system_context()
        full_system = f"{self.PLANNER_INSTRUCTIONS}\n\n{system_ctx}"

        raw = self.engine.infer(full_system, user_prompt)

        # Attempt to parse JSON — handle LLMs that wrap in markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            plan = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback: wrap any plain-text answer into a single echo task
            plan = {
                "tasks": [
                    {
                        "step": 1,
                        "command": f'echo "{cleaned[:200]}"',
                        "description": "LLM returned non-JSON; echoing raw response.",
                    }
                ]
            }

        self.store.log_plan(json.dumps(plan))
        return plan


# ── 3b. Validator Agent ───────────────────────────────────────────────────

class ValidatorAgent:
    """
    Security layer that intercepts the Planner's output and hard-blocks
    destructive or dangerous commands before they reach the Executor.
    """

    @staticmethod
    def validate(plan: dict) -> tuple[list[dict], list[dict]]:
        """
        Split a plan's tasks into (approved, blocked) lists.

        Args:
            plan: The Planner's output dict containing a "tasks" key.

        Returns:
            A tuple of (approved_tasks, blocked_tasks).
        """
        approved: list[dict] = []
        blocked: list[dict] = []

        for task in plan.get("tasks", []):
            cmd = task.get("command", "")
            is_blocked = any(pat.search(cmd) for pat in BLOCKED_PATTERNS)
            if is_blocked:
                task["block_reason"] = "Matched a destructive-command pattern."
                blocked.append(task)
            else:
                approved.append(task)

        return approved, blocked


# ── 3c. Executor Agent ───────────────────────────────────────────────────

class ExecutorAgent:
    """
    Runs validated shell commands via subprocess, captures stdout/stderr,
    and persists results to the Context Store.
    """

    TIMEOUT_SECONDS = 120  # Per-command timeout

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def execute(self, task: dict) -> dict:
        """
        Execute a single task dict and return enriched results.

        Args:
            task: Must contain at least a "command" key.

        Returns:
            The task dict augmented with stdout, stderr, exit_code.
        """
        cmd = task.get("command", "")
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SECONDS,
                cwd=os.getcwd(),
            )
            task["stdout"] = proc.stdout
            task["stderr"] = proc.stderr
            task["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired:
            task["stdout"] = ""
            task["stderr"] = f"[LAMCAP] Command timed out after {self.TIMEOUT_SECONDS}s."
            task["exit_code"] = -1
        except Exception as exc:
            task["stdout"] = ""
            task["stderr"] = f"[LAMCAP] Execution error: {exc}"
            task["exit_code"] = -1

        # Persist to the context store
        self.store.log_execution(
            command=cmd,
            stdout=task["stdout"][:5000],
            stderr=task["stderr"][:5000],
            exit_code=task["exit_code"],
        )
        return task


# ══════════════════════════════════════════════════════════════════════════════
# 4.  MODEL TRIGGER MAPPING & MULTIPLIER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def resolve_model_info(model: str | None = None) -> tuple[str, float]:
    """
    Resolve the active model trigger string and its multiplier.

    Args:
        model: Override for the model trigger.  Falls back to env var,
               then to "gpt-4.1" as the ultimate default.

    Returns:
        (model_name, multiplier)
    """
    model = model or os.environ.get("ANTHROPIC_MODEL", "gpt-4.1")
    multiplier = MODEL_MULTIPLIER_MAP.get(model, 0.0)
    return model, multiplier


def format_multiplier(multiplier: float) -> str:
    """Pretty-print the multiplier value (e.g., '0x', '0.25x', '1x')."""
    if multiplier == int(multiplier):
        return f"{int(multiplier)}x"
    return f"{multiplier}x"


# ══════════════════════════════════════════════════════════════════════════════
# 5.  EXECUTION INTERFACE & UI — Rich Splash + prompt_toolkit REPL
# ══════════════════════════════════════════════════════════════════════════════

LAMCAP_LOGO = """\
[bold purple]
██       ████   ███ ███  █████   ████   █████ 
██      ██  ██  ███████ ██      ██  ██  ██  ██
██      ██████  ██ █ ██ ██      ██████  █████ 
██      ██  ██  ██   ██ ██      ██  ██  ██    
███████ ██  ██  ██   ██  █████  ██  ██  ██    
[/bold purple]"""


def render_splash(model: str, multiplier: float, is_connected: bool) -> None:
    """
    Clear the terminal and paint a premium LAMCAP splash screen using Rich.
    """
    os.system("clear")

    # ── Top welcome panel ─────────────────────────────────────────────────
    console.print(
        Panel(
            "[dim]❖ Welcome to the [bold white]LAMCAP[/bold white] terminal environment![/dim]",
            style="purple",
            box=box.DOUBLE_EDGE,
            expand=True,
        )
    )

    # ── Block-text logo ───────────────────────────────────────────────────
    console.print(LAMCAP_LOGO, justify="center")

    # ── Status line ───────────────────────────────────────────────────────
    cwd = os.getcwd()
    mult_display = format_multiplier(multiplier)
    model_upper = model.upper().replace("-", "-")

    if is_connected:
        bridge_status = "[bold green]Bridge connected.[/bold green]"
    else:
        bridge_status = "[bold red]Bridge disconnected.[/bold red]"

    status = (
        f"{bridge_status}  "
        f"[bold purple]{model_upper}[/bold purple] "
        f"[dim]({mult_display})[/dim]  "
        f"[dim]•[/dim]  "
        f"[dim italic]{cwd}[/dim italic]"
    )
    console.print(Panel(status, style="dim", box=box.ROUNDED, expand=True))

    # ── Quick-help strip ──────────────────────────────────────────────────
    help_items = [
        "[bold purple]help[/bold purple] — show commands",
        "[bold purple]status[/bold purple] — bridge info",
        "[bold purple]history[/bold purple] — past runs",
        "[bold purple]exit[/bold purple] — quit",
    ]
    console.print(
        Columns(help_items, equal=True, expand=True),
        style="dim",
    )
    console.print()


def print_help() -> None:
    """Display a list of built-in REPL commands."""
    table = Table(
        title="[bold purple]LAMCAP Commands[/bold purple]",
        box=box.SIMPLE_HEAVY,
        show_lines=True,
    )
    table.add_column("Command", style="bold purple", no_wrap=True)
    table.add_column("Description", style="dim")
    table.add_row("help", "Show this help table.")
    table.add_row("status", "Display bridge connection info & model multiplier.")
    table.add_row("history", "Show the 10 most recent context entries.")
    table.add_row("clear", "Clear the terminal screen.")
    table.add_row("exit / quit", "Exit LAMCAP.")
    table.add_row("<any text>", "Send a natural-language prompt to the agent pipeline.")
    console.print(table)
    console.print()


def print_status(engine: InferenceEngine) -> None:
    """Print current bridge and model information."""
    mult_display = format_multiplier(engine.multiplier)
    console.print()
    console.print(f"  [bold purple]Proxy URL:[/bold purple]   {engine.base_url}")
    console.print(f"  [bold purple]Model:[/bold purple]       {engine.model}")
    console.print(f"  [bold purple]Multiplier:[/bold purple]  {mult_display}")
    console.print(f"  [bold purple]API Key:[/bold purple]     {'*' * max(0, len(engine.api_key) - 4)}{engine.api_key[-4:]}")
    console.print(f"  [bold purple]Database:[/bold purple]    {DB_PATH}")
    console.print(f"  [bold purple]CWD:[/bold purple]         {os.getcwd()}")
    console.print()


def print_history(store: ContextStore) -> None:
    """Display the most recent history entries in a Rich table."""
    rows = store.recent_history(limit=10)
    if not rows:
        console.print("  [dim]No history yet.[/dim]\n")
        return

    table = Table(
        title="[bold purple]Recent History[/bold purple]",
        box=box.MINIMAL_DOUBLE_HEAD,
        show_lines=True,
        expand=True,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Role", style="bold purple", width=10)
    table.add_column("Content", ratio=1)
    table.add_column("Exit", width=5, justify="center")

    for row in rows:
        content = row.get("prompt") or row.get("command") or (row.get("plan_json") or "")[:80]
        exit_code = str(row["exit_code"]) if row.get("exit_code") is not None else ""
        table.add_row(str(row["id"]), row["role"], content[:120], exit_code)

    console.print(table)
    console.print()


# ══════════════════════════════════════════════════════════════════════════════
# 6.  MAIN REPL LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_agent_pipeline(
    prompt: str,
    engine: InferenceEngine,
    store: ContextStore,
    planner: PlannerAgent,
    validator: ValidatorAgent,
    executor: ExecutorAgent,
) -> None:
    """
    Execute the full Planner → Validator → Executor pipeline for a
    single user prompt, printing rich output at each stage.
    """
    # Log the user prompt
    store.log_user_prompt(prompt, engine.model, engine.multiplier)

    # ── Stage 1: Planning ─────────────────────────────────────────────────
    console.print()
    console.print("[bold purple]⟡ Planner[/bold purple]  [dim]decomposing intent…[/dim]")
    try:
        plan = planner.plan(prompt)
    except ConnectionError as exc:
        console.print(f"[bold red]✗ Bridge Error:[/bold red] {exc}\n")
        return
    except Exception as exc:
        console.print(f"[bold red]✗ Planner Error:[/bold red] {exc}\n")
        return

    tasks = plan.get("tasks", [])
    if not tasks:
        console.print("  [dim]No tasks generated.[/dim]\n")
        return

    # Display the plan
    plan_table = Table(box=box.SIMPLE, show_header=True, expand=True)
    plan_table.add_column("Step", style="bold", width=5, justify="center")
    plan_table.add_column("Command", style="bold purple")
    plan_table.add_column("Description", style="dim")
    for t in tasks:
        plan_table.add_row(str(t.get("step", "?")), t.get("command", ""), t.get("description", ""))
    console.print(plan_table)

    # ── Stage 2: Validation ───────────────────────────────────────────────
    console.print("[bold purple]⟡ Validator[/bold purple]  [dim]scanning for destructive patterns…[/dim]")
    approved, blocked = validator.validate(plan)

    if blocked:
        console.print(f"  [bold red]⚠ BLOCKED {len(blocked)} task(s):[/bold red]")
        for b in blocked:
            console.print(f"    [red]✗ Step {b.get('step')}: {b.get('command')}[/red]")
            console.print(f"      [dim]{b.get('block_reason')}[/dim]")
        console.print()

    if not approved:
        console.print("  [yellow]No tasks approved for execution.[/yellow]\n")
        return

    console.print(f"  [green]✓ {len(approved)} task(s) approved.[/green]")

    # ── Stage 3: Execution ────────────────────────────────────────────────
    console.print("[bold purple]⟡ Executor[/bold purple]  [dim]running commands…[/dim]")
    console.print()

    for task in approved:
        step = task.get("step", "?")
        cmd = task.get("command", "")
        console.print(f"  [bold purple]Step {step}:[/bold purple] [italic]{cmd}[/italic]")

        result = executor.execute(task)
        exit_code = result.get("exit_code", -1)

        if result.get("stdout"):
            for line in result["stdout"].rstrip().split("\n")[:30]:
                console.print(f"    {line}")

        if result.get("stderr"):
            for line in result["stderr"].rstrip().split("\n")[:15]:
                console.print(f"    [red]{line}[/red]")

        if exit_code == 0:
            console.print(f"    [green]✓ exit {exit_code}[/green]")
        else:
            console.print(f"    [red]✗ exit {exit_code}[/red]")
        console.print()

    console.print("[bold purple]⟡ Pipeline complete.[/bold purple]\n")


def main() -> None:
    """
    Entry point: render the splash screen, initialise all layers,
    then drop into the interactive REPL loop.
    """
    # ── Resolve model + multiplier ────────────────────────────────────────
    model, multiplier = resolve_model_info()

    # ── Check connection status ───────────────────────────────────────────
    is_connected = check_bridge_connection(ANTHROPIC_BASE_URL)

    # ── Render splash screen ──────────────────────────────────────────────
    render_splash(model, multiplier, is_connected)

    # ── Initialise core layers ────────────────────────────────────────────
    store = ContextStore()
    engine = InferenceEngine(model=model)
    planner = PlannerAgent(engine=engine, store=store)
    validator = ValidatorAgent()
    executor = ExecutorAgent(store=store)

    # ── Prompt Toolkit session with persistent history ────────────────────
    ptk_style = PTKStyle.from_dict(
        {
            "prompt": "bold purple",
        }
    )
    session: PromptSession = PromptSession(
        history=FileHistory(REPL_HISTORY_PATH),
        style=ptk_style,
    )

    # ── REPL loop ─────────────────────────────────────────────────────────
    try:
        while True:
            try:
                user_input = session.prompt(
                    HTML("<b><purple>lamcap&gt; </purple></b>")
                ).strip()
            except KeyboardInterrupt:
                console.print("\n[dim]Ctrl-C pressed. Type 'exit' to quit.[/dim]")
                continue
            except EOFError:
                break

            if not user_input:
                continue

            lowered = user_input.lower()

            # ── Built-in commands ─────────────────────────────────────────
            if lowered in ("exit", "quit"):
                console.print("[bold purple]Goodbye.[/bold purple] ✦\n")
                break

            if lowered == "help":
                print_help()
                continue

            if lowered == "status":
                print_status(engine)
                continue

            if lowered == "history":
                print_history(store)
                continue

            if lowered == "clear":
                os.system("clear")
                continue

            # ── Agent pipeline ────────────────────────────────────────────
            run_agent_pipeline(
                prompt=user_input,
                engine=engine,
                store=store,
                planner=planner,
                validator=validator,
                executor=executor,
            )

    finally:
        store.close()


# ══════════════════════════════════════════════════════════════════════════════
# 7.  SCRIPT ENTRY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()
