# lamcap: Local Agentic Multi Context Automated Protocol

`lamcap` is a local agentic multi-context automated protocol designed for building and running AI applications on mobile devices (via Termux) and edge devices using local LLMs like Ollama.

## Aim
The goal of `lamcap` is to provide a robust, local-first framework for building agentic AI applications that can run entirely on-device, ensuring privacy, low latency, and offline capabilities.

## Features
- **Local-First**: Built to work with local LLM providers like Ollama.
- **Mobile Ready**: Optimized for environments like Termux on Android.
- **Agentic Capabilities**: Supports AI agents with tool use, RAG, and multi-context handling.
- **Versatile Modes**: Includes CMD mode, REPL mode, and a local HTTP server.

## Getting Started

### How to Clone
To get the latest version of `lamcap` along with your changes, run:
```bash
git clone https://github.com/smdhussain06/LAMCAP.git
cd LAMCAP
```

### Prerequisites
- **Rust**: Required to build the binary.
- **Ollama**: Running locally or on an edge device accessible via network.

#### Install Rust
**On Linux/macOS:**
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

**On Android (Termux):**
```bash
pkg update && pkg upgrade
pkg install rust git clang make binutils
```

### Build & Run
1.  **Build the project:**
    ```bash
    cargo build --release
    ```
2.  **Configure:**
    Edit `config.yaml` to point to your Ollama instance.
3.  **Run:**
    ```bash
    ./target/release/aichat
    ```

## Testing
To test if it's working correctly:
1.  Ensure **Ollama** is running (`ollama serve`).
2.  Run `lamcap` and ask a question:
    ```bash
    ./target/release/aichat "Hello, how are you?"
    ```

## Credits
Based on the excellent [AIChat](https://github.com/sigoden/aichat) tool.