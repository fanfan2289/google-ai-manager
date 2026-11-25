# CLAUDE.md

See [AGENTS.md](./AGENTS.md) for the complete shared architecture, safety invariants, verification commands, and multi-account / multi-provider roadmap for **Google AI Manager** (`google-ai-manager`).

Key reminders when editing this repository:
- Never commit personal emails, OAuth tokens, API keys, `logs/`, `outputs/`, `*.db`, or `.venv-desktop/`.
- Preserve the anti-ghost-billing guarantees in `scripts/colab_gpu_monitor.py` and `scripts/colab_meter.py`.
- Keep `desktop_app/colab_gpu_tools.py` WinForms/WebView2 thread-safe (do not mutate WinForms UI objects from `webview.start(func=...)` background threads).
- Major architectural decisions and progress are also tracked in the Notion knowledge base (**Google AI Manager 知识库**).
