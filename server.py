import asyncio
import dataclasses
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))

from dummy_root import get_app_root
from roktracker.kingdom.additional_data import AdditionalData
from roktracker.kingdom.governor_data import GovernorData
from roktracker.kingdom.scanner import KingdomScanner
from roktracker.utils.general import load_config, ConfigError
from roktracker.utils.output_formats import OutputFormats

logging.basicConfig(
    filename=str(get_app_root() / "kingdom-scanner.log"),
    encoding="utf-8",
    format="%(asctime)s %(module)s %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI()
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# --- Global scanner state ---
_scanner: Optional[KingdomScanner] = None
_scanner_thread: Optional[threading.Thread] = None
_ws: Optional[WebSocket] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_msg_queue: Optional[asyncio.Queue] = None
_continue_event: Optional[threading.Event] = None
_continue_result: dict = {"value": False}


def queue_msg(msg: dict):
    """Thread-safe: enqueue a message to be sent over WebSocket."""
    if _msg_queue and _loop:
        asyncio.run_coroutine_threadsafe(_msg_queue.put(msg), _loop)


@app.get("/")
async def root():
    return HTMLResponse((static_dir / "index.html").read_text())


@app.get("/api/config")
async def api_config():
    try:
        return load_config()
    except Exception as e:
        return {"error": str(e)}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    global _ws, _loop, _msg_queue
    await websocket.accept()
    _ws = websocket
    _loop = asyncio.get_event_loop()
    _msg_queue = asyncio.Queue()

    sender = asyncio.create_task(_sender(websocket))
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg["type"] == "start":
                await _handle_start(msg["config"])
            elif msg["type"] == "stop":
                _handle_stop()
            elif msg["type"] == "continue_response":
                _handle_continue(msg.get("value", False))
    except WebSocketDisconnect:
        if _scanner:
            _scanner.end_scan()
    finally:
        sender.cancel()
        _ws = None


async def _sender(ws: WebSocket):
    while True:
        msg = await _msg_queue.get()
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            break


async def _handle_start(cfg: dict):
    global _scanner_thread
    if _scanner_thread and _scanner_thread.is_alive():
        queue_msg({"type": "error", "message": "A scan is already running."})
        return
    _scanner_thread = threading.Thread(target=_run_scan, args=(cfg,), daemon=True)
    _scanner_thread.start()


def _handle_stop():
    if _scanner:
        _scanner.end_scan()
        queue_msg({"type": "log", "message": "Stop requested — finishing current governor..."})


def _handle_continue(value: bool):
    global _continue_event, _continue_result
    if _continue_event:
        _continue_result["value"] = value
        _continue_event.set()


def _run_scan(cfg: dict):
    global _scanner, _continue_event, _continue_result
    try:
        app_cfg = load_config()
    except ConfigError as e:
        queue_msg({"type": "error", "message": str(e)})
        return

    try:
        app_cfg["scan"]["advanced_scroll"] = cfg.get("advanced_scroll", True)
        app_cfg["scan"]["timings"]["info_close"] = float(cfg.get("info_close", 0.5))
        app_cfg["scan"]["timings"]["gov_close"] = float(cfg.get("gov_close", 1.0))

        kingdom = cfg.get("kingdom_name", "")
        scan_amount = int(cfg.get("people_to_scan", 100))
        adb_port = int(cfg.get("adb_port", 5555))
        resume = cfg.get("resume", False)
        track_inactives = cfg.get("track_inactives", False)
        validate_kills = cfg.get("validate_kills", True)
        reconstruct_fails = cfg.get("reconstruct_kills", True)
        validate_power = cfg.get("validate_power", False)
        power_threshold = int(cfg.get("power_threshold", 100000))

        scan_options = _build_scan_options(
            cfg.get("scan_mode", "full"),
            cfg.get("custom_options", [])
        )
        formats = OutputFormats()
        formats.from_list(cfg.get("formats", ["xlsx"]))

        def ask_continue(msg: str) -> bool:
            global _continue_event, _continue_result
            _continue_event = threading.Event()
            _continue_result = {"value": False}
            queue_msg({"type": "ask_continue", "message": msg})
            _continue_event.wait(timeout=60)
            return _continue_result["value"]

        def state_callback(msg: str):
            queue_msg({"type": "state", "message": msg})

        def output_handler(msg: str):
            queue_msg({"type": "log", "message": msg})

        def gov_callback(gov: GovernorData, extra: AdditionalData):
            queue_msg({
                "type": "governor",
                "data": dataclasses.asdict(gov),
                "progress": {
                    "current": extra.current_governor,
                    "total": extra.target_governor,
                    "eta": extra.eta(),
                    "skipped": extra.skipped_governors,
                },
            })

        _scanner = KingdomScanner(app_cfg, scan_options, adb_port)
        _scanner.set_continue_handler(ask_continue)
        _scanner.set_state_callback(state_callback)
        _scanner.set_output_handler(output_handler)
        _scanner.set_governor_callback(gov_callback)

        queue_msg({"type": "started", "message": f"Scan started — {kingdom}"})
        _scanner.start_scan(
            kingdom, scan_amount, resume, track_inactives,
            validate_kills, reconstruct_fails, validate_power,
            power_threshold, formats,
        )
        queue_msg({"type": "done", "message": "Scan complete!"})

    except Exception as e:
        queue_msg({"type": "error", "message": str(e)})
        logging.exception("Scanner error")
    finally:
        _scanner = None


def _build_scan_options(mode: str, custom: list) -> dict:
    all_keys = [
        "ID", "Name", "Power", "Killpoints", "Alliance",
        "T1 Kills", "T2 Kills", "T3 Kills", "T4 Kills", "T5 Kills",
        "Ranged", "Deads", "Rss Assistance", "Rss Gathered", "Helps",
    ]
    if mode == "full":
        return {k: True for k in all_keys}
    elif mode == "seed":
        seed = {"ID", "Name", "Power", "Killpoints", "Alliance"}
        return {k: k in seed for k in all_keys}
    else:
        return {k: k in custom for k in all_keys}


if __name__ == "__main__":
    import webbrowser
    print("\n  RoK Kingdom Scanner — Web UI")
    print("  Open: http://localhost:8000\n")
    webbrowser.open("http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
