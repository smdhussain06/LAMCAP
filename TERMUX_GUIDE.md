# Termux Setup Guide for lamcap

This guide will walk you through setting up `lamcap` on your Android device using Termux.

## 1. Install Termux
Download Termux from [F-Droid](https://f-droid.org/en/packages/com.termux/) or the [Google Play Store](https://play.google.com/store/apps/details?id=com.termux) (F-Droid is recommended for the latest updates).

## 2. Update Packages
Open Termux and run:
```bash
pkg update && pkg upgrade
```

## 3. Install Dependencies
Install the necessary tools for building Rust projects:
```bash
pkg install rust git clang make binutils
```

## 4. Clone the Repository
```bash
git clone https://github.com/smdhussain06/LAMCAP.git
cd LAMCAP
```

## 5. Build lamcap
This step might take a few minutes depending on your device's performance.
```bash
cargo build --release
```

## 6. Configure Ollama
If you have Ollama running on the same device (via Termux) or on another device in your network:
1.  Open `config.yaml`:
    ```bash
    nano config.yaml
    ```
2.  Ensure `api_base` is correct. If Ollama is on the same device, `http://127.0.0.1:11434` should work.

## 7. Run and Test
```bash
./target/release/aichat "Hello from Termux!"
```

## Troubleshooting
- **Build Failures**: Ensure you have enough storage space (at least 1-2GB for build artifacts) and that your device isn't killing background processes.
- **Connection Errors**: Check if Ollama is running and accessible from Termux.
