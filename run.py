#!/usr/bin/env python3
"""Pessoa launcher — finds a free port and starts uvicorn."""
import socket
import uvicorn


def find_free_port(start: int = 8000, end: int = 8020) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{end}")


if __name__ == "__main__":
    port = find_free_port()
    print(f"Pessoa starting on http://localhost:{port}")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
