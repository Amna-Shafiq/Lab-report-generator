"""Start the lab app:  .venv\\Scripts\\python run.py  [--debug]
Packaged:            DrShafiqAqilClinic.exe

Database: PHARMASOFT_DB environment variable, default data\\lab.db next to the
program (next to the .exe when packaged). PHARMASOFT_NO_BROWSER=1 skips opening
the browser and PHARMASOFT_PORT changes the port (both used for testing).
"""

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from labapp.settings import APP_NAME

HOST = "127.0.0.1"  # localhost only: not reachable from the network
PORT = int(os.environ.get("PHARMASOFT_PORT", "5000"))
URL = f"http://{HOST}:{PORT}/"
FROZEN = getattr(sys, "frozen", False)  # True inside the PyInstaller .exe
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, PORT)) == 0


def _open_browser() -> None:
    if os.environ.get("PHARMASOFT_NO_BROWSER") != "1":
        webbrowser.open(URL)


def main() -> None:
    debug = "--debug" in sys.argv and not FROZEN
    if _port_in_use():
        # Most likely the app is already running (e.g. double-clicked twice): just show it.
        print(f"{APP_NAME} is already running at {URL}")
        _open_browser()
        return

    from labapp.app import create_app  # imported after the check: keeps a second launch fast

    db_path = Path(os.environ.get("PHARMASOFT_DB", APP_DIR / "data" / "lab.db"))
    app = create_app(db_path)
    print(APP_NAME)
    print(f"  Open in browser: {URL}")
    print(f"  Database:        {db_path.resolve()}")
    print("  Keep this window open while using the app. Close it to stop.\n")
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Timer(1.0, _open_browser).start()
    app.run(host=HOST, port=PORT, debug=debug)


if __name__ == "__main__":
    main()
