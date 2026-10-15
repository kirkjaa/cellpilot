"""CellPilot capture studio: a local bridge between the page and a NODAL M1 rig.

    python -m capture.bridge            # http://127.0.0.1:8095, config from capture.json

The bridge speaks the cameras' WebSocket protocol (one JSON envelope per text frame, binary
preview frames) and the rig controller's `cmd.fire`; it never touches the RF dongle itself.
After a take it reads the sync sidecars, computes the clip offsets and writes a session folder
that the main app (`app/`) analyses. Written for the AI Builder Cup 2026 from the protocol
description; no code from the cameras' own software.
"""
