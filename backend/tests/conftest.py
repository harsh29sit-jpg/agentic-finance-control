"""Test bootstrap: one isolated API server + MongoDB database per xdist worker.

Runs at conftest import time (collection) because the legacy suites resolve
BASE_URL at module import. Each worker gets a unique database name, so
parallel workers never collide.
"""
import os
import sys
import socket
import atexit
import threading
import time
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

_worker = os.environ.get("PYTEST_XDIST_WORKER", "gw_main")
TEST_DB = f"recon_test_{_worker}_{uuid.uuid4().hex[:8]}"
MONGO_URL = os.environ.get("TEST_MONGO_URL", "mongodb://localhost:27017")

os.environ["MONGO_URL"] = MONGO_URL
os.environ["DB_NAME"] = TEST_DB
os.environ["JWT_SECRET"] = "test-only-secret"
os.environ["COOKIE_SECURE"] = "false"
# No LLM keys in tests -> agents use deterministic fallbacks by design.


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
BASE_URL = f"http://127.0.0.1:{PORT}"

import uvicorn  # noqa: E402

_server = None
_thread = None


def _start():
    global _server, _thread
    import server as server_module  # noqa: F401  (env already set)

    _server = uvicorn.Server(uvicorn.Config(
        server_module.app, host="127.0.0.1", port=PORT,
        log_level="warning", access_log=False))
    _thread = threading.Thread(target=_server.run, daemon=True)
    _thread.start()

    import requests
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/api/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("test API server failed to become healthy")


def _shutdown():
    try:
        if _server is not None:
            _server.should_exit = True
            _thread.join(timeout=10)
        import pymongo
        pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000) \
            .drop_database(TEST_DB)
    except Exception:
        pass


_start()
atexit.register(_shutdown)

os.environ["REACT_APP_BACKEND_URL"] = BASE_URL
