# LAMCAP — PROJECT CONTEXT

## Project Info
- **Tech Stack:** Python 3.10+, SQLite, Rich, Anthropic/Copilot Proxy
- **Core Architecture:** Recursive Plan-Act-Observe Loop
- **Primary Entry Point:** `lamcap.py`

## Development Rules
- **Coding Style:** Clean, high-performance Python with type hints.
- **Safety:** Never block the Validator Agent logic.
- **Environment:** Compatible with Linux and Android Termux.

## Knowledge
- The proxy server runs on `localhost:4141`.
- All interaction history is stored in `lamcap.db`.
