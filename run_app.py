import os
import sys
import webbrowser
import threading
import time
from app import app  # Import your Flask app


def open_browser():
    time.sleep(1.5)  # Wait for server to start
    webbrowser.open('http://127.0.0.1:5000')


if __name__ == '__main__':
    # Start server in thread
    server_thread = threading.Thread(
        target=lambda: app.run(debug=False, host='127.0.0.1', port=5000, use_reloader=False))
    server_thread.daemon = True
    server_thread.start()

    # Open browser
    threading.Thread(target=open_browser).start()

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down...")
        sys.exit(0)