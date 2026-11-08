# Working on Promptnik

Promptnik is a cross-platform Python desktop client for chat and Ollama System One decisions. Read `README.md` and the relevant implementation before changing behavior. Preserve existing user data and connection compatibility.

## Project layout

- `promptnik/core.py`: endpoint discovery, request formats, streaming, and decision validation.
- `promptnik/storage.py`: local SQLite session history, attachments, connection settings, and credential-store integration.
- `promptnik/ui.py`: PySide6 interface, background jobs, chat, and decision forms.
- `promptnik/assets/`: packaged logo and icon variants. The original logo PNGs are at the project root; `tools/trace_branding.py` rebuilds vector and raster assets.
- `tests/`: standard-library unit tests and offscreen Qt integration tests. `tools/live_smoke.py` performs optional real endpoint checks.

## Development

Python 3.11 or newer is required. Install `requirements.txt` into a virtual environment or the selected Python installation. On Windows, use PowerShell and the interpreter in `.venv\Scripts\python.exe` when that environment is available.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q promptnik tests
.\.venv\Scripts\python.exe promptnik.py
```

Tests set Qt to use its offscreen platform; PySide6 and keyring must be installed for GUI checks. Use a temporary `PROMPTNIK_HOME` or `--data-dir` when testing changes that write application data. Run relevant tests for each change, and verify visual or interactive GUI changes with Qt where practical. Report checks that could not be run.

## Contracts to preserve

- Keep the GUI responsive during network requests and keep tabs and histories independent. Preserve Markdown, inline thinking, images, statistics, accessibility, and light/dark behavior when changing chat presentation.
- Support Responses, Chat Completions, and Messages; preserve provider-specific environment discovery and existing saved connections. Bedrock support is for Mantle bearer-token endpoints. Do not assume every model supports every protocol or capability.
- System One is a structured decision API, separate from chat. Preserve ordered questions and criteria, multiple image attachments, and the image-only state fallback. Verify model capability with a real request before claiming compatibility.
- Keep API keys out of session history, logs, test output, and project files. Use the existing credential-store and explicit plaintext fallback behavior. Never print supplied key files or include keys in exports by default.
- Preserve session and attachment persistence, shared-attachment cleanup, and explicit confirmation for destructive history actions. Treat imported connections and endpoint responses as untrusted input.
- Keep runtime dependencies minimal and compatible with Windows, macOS, and Linux. Pillow and NumPy are only needed to rebuild branding assets, not to run the app. Include packaged assets when changing distribution configuration.
- Do not run paid or live endpoint requests as part of ordinary unit tests. Run `tools/live_smoke.py` or real-model checks only when appropriate and authorized; distinguish mock, offscreen, and live validation.
