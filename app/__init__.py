"""CellPilot server: FastAPI API, analysis pipeline and Gemini 4 Argon calls."""
from __future__ import annotations

try:  # Use the OS certificate store; this PC's antivirus re-signs HTTPS traffic.
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    pass
