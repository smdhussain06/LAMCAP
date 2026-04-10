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

import argparse
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

LAMCAP_LOGO = r"""[bold purple]
  _      ___  __  __ ___   _   ___ 
 | |    / _ \|  \/  | __| /_\ | _ \
 | |__ | (_) | |\/| | _| / _ \|  _/
 |____| \___/|_|  |_|___/_/ \_\_|  
[/bold purple]"""

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

        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
        Includes: CLAUDE.md (if exists) + latest interaction history + cwd snapshot.
        """
        snap = self.snapshot_cwd()
        history = self.recent_history(limit=15)
        
        # Load project-level instructions (Claude Code Style)
        claude_md = ""
        claude_path = Path("CLAUDE.md")
        if claude_path.exists():
            try:
                claude_md = f"\n## PROJECT_CONTEXT (CLAUDE.md)\n{claude_path.read_text()}\n"
            except: pass

        ctx_parts = [
            "## LAMCAP Engine — Runtime Context",
            f"Current Time: {datetime.now(timezone.utc).isoformat()}",
            f"Working Directory: {snap['cwd']}",
            f"File Tree Snapshot: {json.dumps(snap['files'][:60])}",
            claude_md,
            "## Interaction History (Memory)",
        ]

        for row in history:
            if row.get("prompt"):
                ctx_parts.append(f"[user] {row['prompt']}")
            if row.get("plan_json"):
                ctx_parts.append(f"[assistant thoughts] {row['plan_json']}")
            if row.get("command"):
                exit_code = row.get("exit_code", "?")
                ctx_parts.append(f"[system_action] $ {row['command']} (exit {exit_code})")
                if row.get("stdout"):
                    ctx_parts.append(f"  Result: {row['stdout'][:500]}")
                if row.get("stderr"):
                    ctx_parts.append(f"  Error: {row['stderr'][:500]}")

        return "\n".join(ctx_parts)

    # ── Settings & Memory helpers ─────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        """Retrieve a persistent setting from the database."""
        row = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Persist a setting to the database."""
        self.conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def add_memory(self, type_str: str, content: str) -> int:
        """Add a persistent memory / context block."""
        cur = self.conn.execute(
            "INSERT INTO memory (type, content) VALUES (?, ?)",
            (type_str, content),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_memory(self, type_str: str | None = None) -> list[dict]:
        """List persistent memories, optionally filtered by type."""
        if type_str:
            rows = self.conn.execute("SELECT * FROM memory WHERE type = ? ORDER BY id DESC", (type_str,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM memory ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, mem_id: int) -> None:
        """Remove a memory entry by ID."""
        self.conn.execute("DELETE FROM memory WHERE id = ?", (mem_id,))
        self.conn.commit()

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

class BaseEngine:
    """Base class for all LAMCAP inference engines."""
    def __init__(self, model: str):
        self.model = model
        self.multiplier = MODEL_MULTIPLIER_MAP.get(model, 0.0)

    def infer(self, system_prompt: str, user_message: str, max_tokens: int = 4096, stream: bool = False) -> str | Any:
        raise NotImplementedError

class CloudEngine(BaseEngine):
    """Anthropic-compatible client routed through the local bridge."""
    def __init__(
        self,
        base_url: str = ANTHROPIC_BASE_URL,
        api_key: str = ANTHROPIC_API_KEY,
        model: str = ANTHROPIC_MODEL,
    ):
        super().__init__(model)
        self.base_url = base_url
        self.api_key = api_key
        self.client = anthropic.Anthropic(base_url=self.base_url, api_key=self.api_key)

    def infer(self, system_prompt: str, user_message: str, max_tokens: int = 4096, stream: bool = False) -> str | Any:
        import requests
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        data = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
            "stream": stream
        }
        try:
            res = requests.post(f"{self.base_url}/v1/messages", headers=headers, json=data, stream=stream)
            if res.status_code != 200:
                raise ConnectionError(f"Cloud SDK Error {res.status_code}: {res.text}")
                
            if stream:
                def stream_gen():
                    for line in res.iter_lines():
                        if line:
                            line_str = line.decode('utf-8')
                            if line_str.startswith("data: "):
                                try:
                                    chunk = json.loads(line_str[6:])
                                    if chunk.get("type") == "content_block_delta":
                                        yield chunk.get("delta", {}).get("text", "")
                                except: pass
                return stream_gen()
            else:
                out = res.json()
                return "\n".join([c["text"] for c in out.get("content", []) if c.get("type") == "text"])
        except Exception as e:
            raise ConnectionError(f"[LAMCAP] Cloud inference error: {e}")

class LocalEngine(BaseEngine):
    """Local inference engine using Ollama on localhost:11434."""
    def __init__(self, model: str, host: str = "http://localhost:11434"):
        super().__init__(model)
        self.host = host

    def infer(self, system_prompt: str, user_message: str, max_tokens: int = 4096, stream: bool = False) -> str | Any:
        import urllib.request
        import json
        url = f"{self.host}/api/chat"
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "stream": stream,
            "options": {"num_predict": max_tokens}
        }
        try:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"}
            )
            if stream:
                def stream_gen():
                    with urllib.request.urlopen(req, timeout=60) as res:
                        for line in res:
                            if line:
                                body = json.loads(line.decode('utf-8'))
                                yield body.get("message", {}).get("content", "")
                return stream_gen()
            else:
                with urllib.request.urlopen(req, timeout=60) as res:
                    body = json.loads(res.read().decode("utf-8"))
                    return body.get("message", {}).get("content", "")
        except Exception as e:
            raise ConnectionError(f"[LAMCAP] Local Ollama error: {e}")

class InferenceEngine:
    """Factory that returns the appropriate engine based on model prefix."""
    def __new__(cls, model: str | None = None, **kwargs) -> BaseEngine:
        model = model or os.environ.get("ANTHROPIC_MODEL", "gpt-4.1")
        # If model is an Ollama model, use LocalEngine. 
        # For simplicity, we assume models NOT in MODEL_MULTIPLIER_MAP are local unless specified.
        if model not in MODEL_MULTIPLIER_MAP:
            return LocalEngine(model=model)
        return CloudEngine(model=model)

# ══════════════════════════════════════════════════════════════════════════════
# 2b. AUTHENTICATION — GitHub Device Flow (8-digit OTP)
# ══════════════════════════════════════════════════════════════════════════════

import requests

class AuthManager:
    """Handles GitHub Device Flow authentication."""
    CLIENT_ID = "Iv1.b507a08c87ecfe98"  # Official Copilot ID

    @classmethod
    def start_device_flow(cls) -> dict:
        """Request device code and user code from GitHub."""
        try:
            res = requests.post(
                "https://github.com/login/device/code",
                data={"client_id": cls.CLIENT_ID, "scope": "repo,gist,user"},
                headers={"Accept": "application/json"},
                timeout=10
            )
            return res.json()
        except Exception as e:
            return {"error": str(e)}

    @classmethod
    def poll_for_token(cls, device_code: str, interval: int = 5) -> str | None:
        """Poll GitHub for the access token."""
        start_time = time.time()
        while time.time() - start_time < 900:  # 15 min expires
            try:
                res = requests.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": cls.CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
                    },
                    headers={"Accept": "application/json"},
                    timeout=10
                )
                data = res.json()
                if "access_token" in data:
                    return data["access_token"]
                if data.get("error") != "authorization_pending":
                    return None
            except:
                pass
            time.sleep(interval)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 3.  MULTI-AGENT ORCHESTRATION LAYER
# ══════════════════════════════════════════════════════════════════════════════

class PlannerAgent:
    """
    Takes a raw user prompt plus SQLite context and asks the LLM to return
    a JSON-structured list of concrete terminal sub-tasks.
    """

    PLANNER_INSTRUCTIONS = textwrap.dedent("""\
        You are the LAMCAP Agent, a high-level reasoning engine. You operate in a recursive Plan-Act-Observe loop.
        
        CRITICAL UI REQUIREMENT:
        1. YOU MUST ALWAYS START with a <thought> block. 
        2. Inside the <thought> block, explain your reasoning, what you observe, and what you plan to do in a professional, helpful tone.
        3. DO NOT output code or JSON inside the <thought> block.
        4. AFTER the </thought> block, output exactly ONE JSON tool call.
        
        GOAL: Accomplish the user's task using the terminal.
        
        RULES:
        - Analyze the current state, file tree, and history before every step.
        - If you need to run a command, return JSON with "action": "run" and "command": "<cmd>".
        - PERSISTENCE: If the user asks for a server or long-running process, you MUST use 'nohup' and '&' so it survives the session (e.g., 'nohup python3 -m http.server 8080 &').
        - DIRECTORIES: Be extremely careful with paths. If you create a website in a folder, use the '--directory' flag or 'cd' into it first.
        - PREVIEW: When hosting is ready, provide a direct URL like http://localhost:8080.
        - If the task is finished, return "status": "FINISHED" and a summary.
        - NEVER return multiple commands at once. Wait for observation.
        
        EXAMPLE OUTPUT:
        <thought>
        I see that the project uses Python. I will check the requirements.txt to understand the dependencies.
        </thought>
        {
          "action": "run",
          "command": "cat requirements.txt",
          "description": "Reading dependencies."
        }
    """)

    def __init__(self, engine: InferenceEngine, store: ContextStore) -> None:
        self.engine = engine
        self.store = store

    def plan(self, user_prompt: str) -> dict:
        """
        Produce a single iterative reasoning step with a filtered thought display.
        """
        from rich.live import Live
        from rich.markdown import Markdown
        from rich.panel import Panel

        system_ctx = self.store.build_system_context()
        full_system = f"{self.PLANNER_INSTRUCTIONS}\n\n{system_ctx}"

        raw = ""
        display_text = ""
        
        # Create a dynamic panel that updates live as chunks stream in
        with Live(Panel(Markdown(""), title="[bold purple]⟡ Agent Reasoning[/bold purple]", border_style="purple"), refresh_per_second=15) as live:
            for chunk in self.engine.infer(full_system, user_prompt, stream=True):
                raw += chunk
                
                # Extract thoughts on the fly to show ONLY the human-friendly part
                thought_match = re.search(r"<thought>(.*?)(?:</thought>|$)", raw, flags=re.DOTALL)
                if thought_match:
                    display_text = thought_match.group(1).strip()
                    live.update(Panel(Markdown(display_text), title="[bold purple]⟡ Agent Reasoning[/bold purple]", border_style="purple"))

        # Clean the output by stripping out the <thought> block to get the JSON tool call
        cleaned = re.sub(r"<thought>.*?</thought>", "", raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            plan = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback: If the model didn't provide JSON (e.g. just a text greeting)
            # We treat this as a completion step with the text as the summary.
            plan = {
                "status": "FINISHED",
                "summary": cleaned or display_text
            }

        # Final safety check: if both action and status are missing, it's a finish.
        if not plan.get("action") and not plan.get("status"):
            plan["status"] = "FINISHED"
            plan["summary"] = cleaned or display_text

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
        """Execute a task and handle backgrounding detection."""
        cmd = task.get("command", "").strip()
        is_bg = cmd.endswith("&")
        
        try:
            if is_bg:
                # Use Popen to detach background processes
                # Redirect to avoid hanging on pipes
                safe_cmd = f"nohup {cmd} > /dev/null 2>&1" if ">" not in cmd else cmd
                proc = subprocess.Popen(
                    safe_cmd,
                    shell=True,
                    start_new_session=True, # Detach from terminal session
                    cwd=os.getcwd()
                )
                task["stdout"] = f"[LAMCAP] Persistent background job started (PID: {proc.pid})"
                task["stderr"] = ""
                task["exit_code"] = 0
            else:
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
        except Exception as exc:
            task["stdout"] = ""
            task["stderr"] = f"[LAMCAP] Execution error: {exc}"
            task["exit_code"] = -1

        self.store.log_execution(cmd, task["stdout"], task["stderr"], task["exit_code"])
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
# 5.  UI COMPONENTS — Splash, Tables & Menus
# ══════════════════════════════════════════════════════════════════════════════

class MenuManager:
    """Handles the top-level LAMCAP menu and its sub-sections."""

    @staticmethod
    def show_home(store: ContextStore, is_connected: bool) -> str:
        """Display the primary dashboard/menu."""
        os.system("clear")
        console.print(Panel("[bold purple]LAMCAP[/bold purple] Automation Engine", style="purple", box=box.DOUBLE_EDGE, expand=True))
        console.print(LAMCAP_LOGO, justify="center")
        
        status_color = "green" if is_connected else "red"
        status_text = "Connected" if is_connected else "Disconnected"
        
        trigger = store.get_setting("trigger_name", "LAMCAP")
        
        console.print(Panel(
            f"  [bold]• Status:[/bold]      [{status_color}]{status_text}[/{status_color}]\n"
            f"  [bold]• Trigger:[/bold]     [purple]{trigger}[/purple]\n"
            f"  [bold]• Directory:[/bold]   [dim]{os.getcwd()}[/dim]",
            title="System Snapshot", box=box.ROUNDED, expand=True
        ))

        menu_items = (
            "[bold white]1) [purple]Cloud[/purple][/bold white]           — Claude/GPT/Gemini Bridge\n"
            "[bold white]2) [purple]Local[/purple][/bold white]           — Ollama (Offline Mode)\n"
            "[bold white]3) [purple]Authentication[/purple][/bold white]  — GitHub Link (8-digit OTP)\n"
            "[bold white]4) [purple]Settings[/purple][/bold white]        — Triggers & Context Memory\n"
            "[bold white]5) [purple]REPL[/purple][/bold white]            — Enter the Agent Shell\n"
            "[bold white]0) [purple]Exit[/purple][/bold white]"
        )
        console.print(Panel(menu_items, title="Main Menu", border_style="dim", expand=True))
        
        try:
            choice = console.input("\n[bold purple]lamcap> [/bold purple]").strip()
            return choice
        except (KeyboardInterrupt, EOFError):
            return "0"

    @staticmethod
    def show_cloud() -> str | None:
        """Menu to select cloud models."""
        table = Table(title="Claude Code Models (Copilot Bridge)", box=box.SIMPLE_HEAVY, expand=True)
        table.add_column("ID", style="bold", width=4)
        table.add_column("Model Trigger", style="purple")
        table.add_column("Multiplier", justify="right")
        
        models = [
            ("1", "gpt-4.1", "0x"),
            ("2", "gpt-4o", "0x"),
            ("3", "grok-code-fast-1", "0.25x"),
            ("4", "claude-haiku-4.5", "0.33x"),
            ("5", "gemini-3-flash-preview", "0.33x"),
            ("6", "gemini-3.1-pro-preview", "1x"),
        ]
        for m in models:
            table.add_row(*m)
        
        console.print(table)
        try:
            choice = console.input("\n[bold purple]Select Model ID (or 'b' to go back): [/bold purple]").strip()
            if choice.lower() == 'b': return None
            idx = int(choice) - 1 if choice.isdigit() and 0 < int(choice) <= len(models) else None
            return models[idx][1] if idx is not None else None
        except:
            return None

    @staticmethod
    def show_local() -> str | None:
        """Ollama model manager."""
        console.print("\n[bold]Checking local Ollama models...[/bold]")
        try:
            res = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                console.print(res.stdout)
            else:
                raise Exception("Ollama not responding")
        except:
            console.print("[yellow]Ollama not detected or not running.[/yellow]")
            console.print("\n[bold white]Suggested for LAMCAP workflow:[/bold white]")
            console.print("  • llama3 (8b)  — General purpose")
            console.print("  • mistral     — Fast, high instructions")
            console.print("  • phi3        — Lightweight for mobile")
            
        try:
            choice = console.input("\n[bold purple]Enter model name to use (or 'pull <name>' to install): [/bold purple]").strip()
            if not choice: return None
            if choice.startswith("pull "):
                model_to_pull = choice.replace("pull ", "")
                console.print(f"Installing {model_to_pull} via Ollama...")
                subprocess.run(["ollama", "pull", model_to_pull])
                return model_to_pull
            return choice
        except:
            return None

    @staticmethod
    def show_auth(store: ContextStore) -> None:
        """8-digit OTP GitHub flow."""
        console.print("\n[bold purple]GitHub Authentication — Link Proxy / LAMCAP Bridge[/bold purple]")
        data = AuthManager.start_device_flow()
        if "error" in data:
            console.print(f"[red]Error:[/red] {data.get('error')}")
            console.print("[dim]Press Enter to return.[/dim]")
            try: console.input()
            except: pass
            return

        user_code = data.get('user_code', 'ERROR')
        uri = data.get('verification_uri', 'https://github.com/login/device')
        
        console.print(Panel(
            f"1. Open: [link={uri}]{uri}[/link]\n"
            f"2. Enter Code: [bold cyan]{user_code}[/bold cyan]\n"
            "\n[dim italic]Waiting for authorization...[/dim italic]",
            title="GitHub Identity Link", border_style="purple", expand=True
        ))
        
        token = AuthManager.poll_for_token(data.get('device_code', ''), data.get('interval', 5))
        if token:
            store.set_setting("gh_token", token)
            console.print("\n[bold green]✓ Successfully authenticated![/bold green]")
        else:
            console.print("\n[red]✗ Authentication failed or timed out.[/red]")
        
        try: console.input("\n[dim]Press Enter to continue...[/dim]")
        except: pass

    @staticmethod
    def show_settings(store: ContextStore) -> None:
        """Trigger & Memory management."""
        while True:
            os.system("clear")
            current_trigger = store.get_setting("trigger_name", "LAMCAP")
            
            console.print(Panel(
                f"[bold white]1. Update Trigger:[/bold white] Currently '[purple]{current_trigger}[/purple]'\n"
                f"[bold white]2. Add Prompt Memory:[/bold white] inject persistent context\n"
                f"[bold white]3. List/Clear Memory:[/bold white]\n"
                f"[bold white]0. Go Back[/bold white]",
                title="Settings & Context Memory", expand=True
            ))
            
            try:
                sub = console.input("\n[bold purple]settings> [/bold purple]").strip()
                if sub == "1":
                    new_t = console.input("New trigger name: ").strip()
                    if new_t: store.set_setting("trigger_name", new_t)
                elif sub == "2":
                    content = console.input("Enter natural text prompt for memory:\n")
                    if content: store.add_memory("prompt", content)
                elif sub == "3":
                    mems = store.list_memory()
                    if not mems:
                        console.print("  [dim]No memory entries found.[/dim]")
                    for m in mems:
                        console.print(f"  [{m['id']}] ({m['ts']}) [dim]{m['content'][:60]}...[/dim]")
                    cid = console.input("\nEnter ID to delete (or Enter for none): ").strip()
                    if cid.isdigit(): store.delete_memory(int(cid))
                elif sub == "0":
                    break
            except (KeyboardInterrupt, EOFError):
                break

def render_splash(model: str, multiplier: float, is_connected: bool) -> None:
    """Clear the terminal and paint a premium LAMCAP splash screen."""
    os.system("clear")
    console.print(Panel("[dim]❖ Welcome to the [bold white]LAMCAP[/bold white] terminal environment![/dim]", style="purple", box=box.DOUBLE_EDGE, expand=True))
    console.print(LAMCAP_LOGO, justify="center")
    
    cwd = os.getcwd()
    mult_display = format_multiplier(multiplier)
    model_upper = model.upper().replace("-", "-")
    status_str = "[green]Bridge connected.[/green]" if is_connected else "[red]Bridge disconnected.[/red]"

    status = (
        f"{status_str}  "
        f"[bold purple]{model_upper}[/bold purple] "
        f"({mult_display})  •  [dim italic]{cwd}[/dim italic]"
    )
    console.print(Panel(status, style="dim", box=box.ROUNDED, expand=True))

    help_items = [
        "[bold purple]help[/bold purple] — info",
        "[bold purple]status[/bold purple] — stats",
        "[bold purple]history[/bold purple] — runs",
        "[bold purple]exit[/bold purple] — quit",
    ]
    console.print(Columns(help_items, equal=True, expand=True), style="dim")
    console.print()


def print_help() -> None:
    """Display a list of built-in REPL commands."""
    table = Table(title="[bold purple]LAMCAP Commands[/bold purple]", box=box.SIMPLE_HEAVY, show_lines=True)
    table.add_column("Command", style="bold purple", no_wrap=True)
    table.add_column("Description", style="dim")
    table.add_row("help", "Show this help table.")
    table.add_row("status", "Display bridge connection info & model multiplier.")
    table.add_row("history", "Show the 10 most recent context entries.")
    table.add_row("clear", "Clear the terminal screen.")
    table.add_row("exit / quit", "Exit LAMCAP.")
    table.add_row("<any text>", "Send a natural-language prompt to the agent pipeline.")
    console.print(table)


def print_status(engine: BaseEngine) -> None:
    """Print current bridge and model information."""
    mult_display = format_multiplier(engine.multiplier)
    console.print()
    if hasattr(engine, "base_url"):
        console.print(f"  [bold purple]Proxy URL:[/bold purple]   {engine.base_url}")
    console.print(f"  [bold purple]Model:[/bold purple]       {engine.model}")
    console.print(f"  [bold purple]Multiplier:[/bold purple]  {mult_display}")
    console.print(f"  [bold purple]Database:[/bold purple]    {DB_PATH}")
    console.print()


def print_history(store: ContextStore) -> None:
    """Display the most recent history entries."""
    rows = store.recent_history(limit=10)
    if not rows:
        console.print("  [dim]No history yet.[/dim]\n")
        return
    table = Table(title="[bold purple]Recent History[/bold purple]", box=box.MINIMAL_DOUBLE_HEAD, expand=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Role", style="bold purple", width=10)
    table.add_column("Content", ratio=1)
    table.add_column("Exit", width=5, justify="center")
    for r in rows:
        content = r.get("prompt") or r.get("command") or (r.get("plan_json") or "")[:80]
        table.add_row(str(r["id"]), r["role"], content[:120], str(r.get("exit_code", "")))
    console.print(table)


# ══════════════════════════════════════════════════════════════════════════════
# 6.  EXECUTION PIPELINE & REPL
# ══════════════════════════════════════════════════════════════════════════════

def run_agent_pipeline(prompt: str, engine: BaseEngine, store: ContextStore, planner: PlannerAgent, validator: ValidatorAgent, executor: ExecutorAgent, auto_accept: bool = False) -> None:
    """The Recursive Plan-Act-Observe Loop with Human-in-the-Loop Feedback."""
    
    current_prompt = prompt
    iteration = 0
    max_iterations = 25
    
    # Permission Mode: 
    permission_mode = store.get_setting("execution_mode", "NORMAL")
    if auto_accept: 
        permission_mode = "AUTO"
    
    while iteration < max_iterations:
        iteration += 1
        
        # 1. THINK & DECIDE NEXT STEP
        try:
            step_plan = planner.plan(current_prompt)
        except Exception as exc:
            console.print(f"[bold red]✗ Agent Error:[/bold red] {exc}\n")
            break
            
        # 2. STATUS CHECK
        if step_plan.get("status") == "FINISHED" or not step_plan.get("action"):
            summary = step_plan.get('summary', 'No summary provided.')
            console.print(f"\n[bold green]✓ Agent:[/bold green] {summary}\n")
            break
            
        action_cmd = step_plan.get("command")
        if not action_cmd: break

        # Fix: If user wants a server, remind the agent to use the RIGHT directory and detached mode
        if "http.server" in action_cmd and "nohup" not in action_cmd:
            # We don't modify the cmd directly, but we signal the agent in the next iteration if it fails
            pass
            
        # 3. VALIDATION & SECURITY
        temp_plan = {"tasks": [step_plan]}
        approved, blocked = validator.validate(temp_plan)
        
        if blocked:
            console.print(f"  [red]✗ Security Blocked:[/red] {action_cmd}")
            break

        # 4. INTERACTIVE APPROVAL & HITL FEEDBACK
        if permission_mode != "AUTO":
            user_input = console.input(f"\n  [bold yellow]?[/bold yellow] Allow command: [italic]{action_cmd}[/italic]? [dim](y/n/conversational feedback)[/dim]\n  [bold cyan]lamcap>[/bold cyan] ").strip()
            
            # Simple 'n' or empty means abort
            if not user_input or user_input.lower() in ('n', 'no', 'quit', 'exit'):
                console.print("[dim]✗ Session closed.[/dim]\n")
                break
            
            # If user types 'auto', switch mode for this session
            if user_input.lower() == 'auto':
                permission_mode = "AUTO"
                console.print("[bold green]⟡ Switched to AUTO-ACCEPT mode for this session.[/bold green]")
            elif user_input.lower() != 'y':
                console.print(f"\n[bold blue]⟡ Pivoting based on feedback:[/bold blue] [italic]{user_input}[/italic]")
                current_prompt = f"USER FEEDBACK: {user_input}\n\nPrevious intent was to run: {action_cmd}\nBut do not run that, follow the new feedback instead."
                continue

        # 5. EXECUTION & OBSERVATION
        console.print(f"  [bold purple]Executing:[/bold purple] [dim]{action_cmd}[/dim]")
        with console.status("[dim]Working...[/dim]", spinner="dots"):
            result = executor.execute(step_plan)
            
        store.log_execution(action_cmd, result.get("stdout", ""), result.get("stderr", ""), result.get("exit_code", 0))
        
        # Display to User (Truncated for readability)
        if result.get("stdout"): 
            out = result['stdout']
            display_out = out[:1200] + ("\n[dim]...(output truncated in display)...[/dim]" if len(out) > 1200 else "")
            console.print(f"    [dim]{display_out}[/dim]")
        if result.get("stderr"):
            console.print(f"    [red]{result['stderr'][:1000]}[/red]")
            
        # THE FIX: Return MUCH more data to the Agent's next prompt so it sees the 'Address already in use' error
        obs_stdout = result.get('stdout', '')[:5000]
        obs_stderr = result.get('stderr', '')[:5000]
        
        current_prompt = f"COMMAND_RESULT:\n$ {action_cmd}\nEXIT_CODE: {result.get('exit_code', 0)}\nSTDOUT: {obs_stdout}\nSTDERR: {obs_stderr}\n\nDecide the next step. If 'Address already in use', try a different port."

    if iteration >= max_iterations:
        console.print("[yellow]! Safety cap reached.[/yellow]")
    
    console.print("[bold purple]⟡ Session Closed.[/bold purple]\n")


def main() -> None:
    """Main execution loop."""
    parser = argparse.ArgumentParser(description="LAMCAP Agentic Automation")
    parser.add_argument("-y", "--auto", action="store_true", help="Auto-accept commands")
    args, unknown = parser.parse_known_args()

    store = ContextStore()
    model = store.get_setting("last_model", ANTHROPIC_MODEL)
    
    first_prompt = None

    while True:
        is_connected = check_bridge_connection(ANTHROPIC_BASE_URL)
        choice = MenuManager.show_home(store, is_connected)
        
        if choice == "1":
            m = MenuManager.show_cloud()
            if m: store.set_setting("last_model", m); model = m
        elif choice == "2":
            m = MenuManager.show_local()
            if m: store.set_setting("last_model", m); model = m
        elif choice == "3":
            MenuManager.show_auth(store)
        elif choice == "4":
            MenuManager.show_settings(store)
        elif choice == "5":
            break
        elif choice == "0":
            console.print("Goodbye. ✦")
            return
        elif choice:  # If what they typed is anything else, parse it as a user prompt
            first_prompt = choice
            break

    multiplier = MODEL_MULTIPLIER_MAP.get(model, 0.0)
    render_splash(model, multiplier, is_connected)
    
    engine = InferenceEngine(model=model)
    planner = PlannerAgent(engine, store)
    validator = ValidatorAgent()
    executor = ExecutorAgent(store)

    trigger_name = store.get_setting("trigger_name", "LAMCAP")
    session = PromptSession(history=FileHistory(REPL_HISTORY_PATH))

    if first_prompt:
        run_agent_pipeline(first_prompt, engine, store, planner, validator, executor, auto_accept=args.auto)

    while True:
        try:
            prompt_html = f"<b><ansipurple>{trigger_name.lower()}&gt; </ansipurple></b>"
            user_input = session.prompt(HTML(prompt_html)).strip()
            if not user_input: continue
            
            lowered = user_input.lower()
            if lowered in ("exit", "quit"): break
            elif lowered == "help": print_help()
            elif lowered == "status": print_status(engine)
            elif lowered == "history": print_history(store)
            elif lowered == "clear": os.system("clear")
            else: run_agent_pipeline(user_input, engine, store, planner, validator, executor, auto_accept=args.auto)
        except (KeyboardInterrupt, EOFError):
            break
    
    store.close()
    console.print("\nGoodbye. ✦")

# ══════════════════════════════════════════════════════════════════════════════
# 7.  SCRIPT ENTRY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()
