# LAMCAP — Local Agentic Multi-Context Automation Protocol

A stateful, multi-agent CLI automation engine that runs entirely on your phone via Termux. No cloud dependencies — inference routes through a local Copilot-to-Anthropic proxy tunnel.

```
██       ████   ███ ███  █████   ████   █████ 
██      ██  ██  ███████ ██      ██  ██  ██  ██
██      ██████  ██ █ ██ ██      ██████  █████ 
██      ██  ██  ██   ██ ██      ██  ██  ██    
███████ ██  ██  ██   ██  █████  ██  ██  ██    
```

---

## 📱 Termux Installation (Step-by-Step)

### Step 1 — Install Termux

Download **Termux** from [F-Droid](https://f-droid.org/en/packages/com.termux/) (recommended) or the Play Store.

> ⚠️ The F-Droid version is kept up-to-date. The Play Store version is often outdated and may cause issues.

---

### Step 2 — Update Termux packages

Open Termux and run:

```bash
pkg update && pkg upgrade -y
```

---

### Step 3 — Install required system packages

```bash
pkg install python git -y
```

This installs Python 3.11+ and Git.

---

### Step 4 — Grant storage access (optional but recommended)

```bash
termux-setup-storage
```

Tap **Allow** when prompted. This lets LAMCAP access `~/storage/shared/` (your phone's internal storage).

---

### Step 5 — Clone the repository

```bash
git clone https://github.com/smdhussain06/LAMCAP.git
cd LAMCAP
```

---

### Step 6 — Install Python dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `anthropic` — inference client (routed to your local proxy)
- `rich` — premium terminal UI
- `prompt_toolkit` — interactive REPL with history

---

### Step 7 — Set up your environment variables

You need to tell LAMCAP where your proxy tunnel is running and which model to use.

```bash
export ANTHROPIC_BASE_URL="http://localhost:4141"
export ANTHROPIC_API_KEY="your-api-key-here"
export ANTHROPIC_MODEL="gpt-4.1"
```

To make these **persistent** across Termux sessions, add them to your shell profile:

```bash
echo 'export ANTHROPIC_BASE_URL="http://localhost:4141"' >> ~/.bashrc
echo 'export ANTHROPIC_API_KEY="your-api-key-here"' >> ~/.bashrc
echo 'export ANTHROPIC_MODEL="gpt-4.1"' >> ~/.bashrc
source ~/.bashrc
```

#### Available Models & Multipliers

| Model Trigger              | Student Token Multiplier |
|----------------------------|--------------------------|
| `gpt-4.1`                 | 0x                       |
| `gpt-4o`                  | 0x                       |
| `grok-code-fast-1`        | 0.25x                    |
| `claude-haiku-4.5`        | 0.33x                    |
| `gemini-3-flash-preview`  | 0.33x                    |
| `gemini-3.1-pro-preview`  | 1x                       |

---

### Step 8 — Start your Copilot-to-Anthropic proxy

Make sure your proxy tunnel is running on `localhost:4141` **before** launching LAMCAP. 

> The proxy bridges Copilot inference to an Anthropic-compatible API so your phone doesn't burn through local compute.

---

### Step 9 — Launch LAMCAP 🚀

```bash
python lamcap.py
```

You'll see the splash screen, then the interactive prompt:

```
lamcap> 
```

---

## 🎮 Built-in Commands

| Command       | What it does                                    |
|---------------|-------------------------------------------------|
| `help`        | Show all available commands                     |
| `status`      | Display bridge connection info & model details  |
| `history`     | Show recent execution history from SQLite       |
| `clear`       | Clear the terminal screen                       |
| `exit`/`quit` | Exit LAMCAP                                     |
| *any text*    | Send a natural-language prompt to the agent pipeline |

---

## 🏗️ Architecture

```
User Prompt
    │
    ▼
┌─────────────┐     ┌───────────────┐     ┌──────────────┐
│   Planner   │────▶│   Validator   │────▶│   Executor   │
│   Agent     │     │   Agent       │     │   Agent      │
└─────────────┘     └───────────────┘     └──────────────┘
    │                     │                      │
    ▼                     ▼                      ▼
 JSON plan          Block dangerous        subprocess.run()
 from LLM           commands (rm -rf,      ➜ capture output
                    chmod 777, dd,         ➜ save to SQLite
                    fork bombs)
```

All context flows through **SQLite** (`lamcap.db`) — prompts, plans, command outputs, and filesystem snapshots are persisted across sessions.

---

## 🔧 Troubleshooting

### `pip install` fails with build errors
```bash
pkg install build-essential libffi openssl -y
pip install -r requirements.txt
```

### "Proxy bridge unreachable" error
Your tunnel on `localhost:4141` isn't running. Start it first, then relaunch LAMCAP.

### Permission denied on storage
Run `termux-setup-storage` again and grant access.

### Python version too old
```bash
python --version   # needs 3.11+
pkg install python -y
```

---

## 📄 License

MIT
