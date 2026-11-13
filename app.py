"""Skroutz Tools: starts the Deal Finder and the Laptop Picker together and opens your browser.

Run:  python app.py   (or double-click Start.bat on Windows)
  Deal Finder   -> http://127.0.0.1:8765
  Laptop Picker -> http://127.0.0.1:8766
Both are only reachable from this PC. Ctrl+C (or closing the window) stops them.
"""
import os
import threading
import webbrowser
from http.server import ThreadingHTTPServer

import laptop_picker
import skroutz_web

if __name__ == "__main__":
    laptop_picker.selfcheck()
    ThreadingHTTPServer.allow_reuse_address = False  # refuse to share a port with an old copy (see skroutz_web.py)
    servers = []
    for tool in (skroutz_web, laptop_picker):
        try:
            servers.append(ThreadingHTTPServer(("127.0.0.1", tool.PORT), tool.Handler))
        except OSError:
            raise SystemExit(f"Port {tool.PORT} is busy: a copy is probably already running. Close it and try again.")
    threading.Thread(target=servers[1].serve_forever, daemon=True).start()
    print(f"Deal Finder   -> http://127.0.0.1:{skroutz_web.PORT}")
    print(f"Laptop Picker -> http://127.0.0.1:{laptop_picker.PORT}")
    print("Ctrl+C or close this window to stop.")
    if not os.environ.get("NO_BROWSER"):
        webbrowser.open(f"http://127.0.0.1:{skroutz_web.PORT}")
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("Stopped")
