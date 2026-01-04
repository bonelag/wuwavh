# WuWa Mod Tool (Tauri Port)

This is a port of the Python/Tkinter translation tool to Tauri v2 + SolidJS + Rust.

## Prerequisites

- **Node.js**: v18 or later
- **Rust**: Latest stable
- **VS C++ Build Tools** (on Windows) or build-essential/webkit2gtk (on Linux)

## Setup

1. Install dependencies:
   ```bash
   npm install
   ```

2. Run in development mode:
   ```bash
   npm run tauri dev
   ```

3. Build for production:
   ```bash
   npm run tauri build
   ```

## Structure

- `src/`: Frontend code (SolidJS, TailwindCSS)
- `src-tauri/`: Backend code (Rust)
  - `src/translator.rs`: Core translation logic (threading, API calls)
  - `src/lib.rs`: Command registration

## Features

- Multi-threaded translation
- Streaming API support (Mistral/OpenAI compatible)
- Real-time progress tracking
- Configurable settings (API Key, Model, Prompts)
