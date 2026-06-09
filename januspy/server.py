"""FastAPI web server: a browser dashboard over the real-time modem.

Holds a single shared :class:`AudioEngine`. Engine callbacks (decoded packets,
waterfall rows, detector events, TX summaries) run on audio/reader threads and are
marshalled onto the asyncio loop, then broadcast to all connected WebSocket clients.
Clients send commands (start/stop RX, transmit, query devices) over the same socket.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .audio import AudioEngine, list_devices
from .engine import MAX_DEFAULT_CARGO, Detection, JanusEngine
from .psets import load_psets

_WEB_DIR = Path(__file__).resolve().parent / "web"


def packet_to_dict(det: Detection) -> dict:
    """Serialise a Detection (packet + receiver state) for the WebSocket."""
    p, s = det.packet, det.state
    return {
        "time": time.time(),
        "payload": p.payload,
        "cargo_ascii": p.cargo_ascii,
        "cargo_hex": p.cargo_hex,
        "class_id": p.class_id,
        "class_id_name": p.class_id_name,
        "app_type": p.app_type,
        "bytes_hex": p.bytes_hex,
        "crc_ok": p.crc_ok,
        "payload_ok": p.payload_ok,
        "cargo_size": p.cargo_size,
        "fields": p.fields,
        "after": s.after,
        "gamma": s.gamma,
        "speed": s.speed,
        "rssi": s.rssi,
        "snr": s.snr,
    }


class Hub:
    """Bridges thread-land engine callbacks to async WebSocket broadcast."""

    def __init__(self, engine: JanusEngine | None = None):
        self.engine = engine or JanusEngine()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        self.audio = AudioEngine(
            self.engine,
            on_packet=lambda d: self._emit({"type": "packet", "packet": packet_to_dict(d)}),
            on_waterfall=lambda row: self._emit({"type": "waterfall", "row": row}),
            on_event=lambda e: self._emit({"type": "event", "message": e}),
            on_tx=lambda info: self._emit({"type": "tx", "info": info}),
            on_level=lambda lv: self._emit({"type": "level", "level": lv}),
        )

    def _emit(self, message: dict) -> None:
        """Schedule a broadcast from a worker thread onto the event loop."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._broadcast_nowait, message)

    def _broadcast_nowait(self, message: dict) -> None:
        """Fire-and-forget broadcast (must run on the event loop)."""
        asyncio.ensure_future(self.broadcast(message))

    async def broadcast(self, message: dict) -> None:
        """Send a JSON message to every connected client, dropping dead sockets."""
        if not self.clients:
            return
        data = json.dumps(message)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def status(self) -> dict:
        """Current engine/receiver status broadcast to clients."""
        return {
            "type": "status",
            "running": self.audio.running,
            "pset_id": self.audio.pset_id,
            "fs": self.audio.fs,
            "reference": str(self.engine.config.tx_bin),
            "max_cargo": MAX_DEFAULT_CARGO,
            "verify": self.audio.verify,
        }


def create_app(engine: JanusEngine | None = None) -> FastAPI:
    """Build the FastAPI application backed by a single shared modem engine."""
    hub = Hub(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Capture the event loop on startup and release audio on shutdown."""
        hub.loop = asyncio.get_running_loop()
        yield
        hub.audio.close()

    app = FastAPI(title="januspy modem", lifespan=lifespan)
    app.state.hub = hub

    @app.get("/api/devices")
    async def devices():
        """List audio input/output devices."""
        return JSONResponse(list_devices())

    @app.get("/api/psets")
    async def psets():
        """List the available parameter sets."""
        return JSONResponse(load_psets(hub.engine.config.pset_file))

    @app.get("/api/status")
    async def status():
        """Current modem status."""
        return JSONResponse(hub.status())

    @app.get("/api/waterfall")
    async def waterfall():
        """Current spectrogram configuration."""
        return JSONResponse({"type": "waterfall_config", **hub.audio.waterfall_config()})

    @app.get("/")
    async def index():
        """Serve the single-page web UI.

        ``no-cache`` makes the browser revalidate on every load (cheap 304 when the
        file is unchanged), so UI updates always show up without a hard refresh or a
        ``?v=`` cache-buster.
        """
        return FileResponse(_WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    async def favicon():
        """Serve the site icon."""
        return FileResponse(_WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        """Per-client WebSocket: push state/events, receive commands."""
        await websocket.accept()
        hub.clients.add(websocket)
        await websocket.send_text(json.dumps(hub.status()))
        await websocket.send_text(json.dumps({"type": "devices", "devices": list_devices()}))
        await websocket.send_text(json.dumps({"type": "psets", "psets": load_psets(hub.engine.config.pset_file)}))
        await websocket.send_text(json.dumps({"type": "waterfall_config", **hub.audio.waterfall_config()}))
        try:
            while True:
                raw = await websocket.receive_text()
                await _handle_command(hub, json.loads(raw))
        except (WebSocketDisconnect, RuntimeError):
            pass  # client went away / socket closed during shutdown
        except Exception:
            pass
        finally:
            hub.clients.discard(websocket)

    if _WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")

    return app


async def _handle_command(hub: Hub, cmd: dict) -> None:
    """Apply a command message received from a WebSocket client."""
    action = cmd.get("action")
    try:
        if action == "start_rx":
            await asyncio.to_thread(
                hub.audio.start_rx,
                input_device=cmd.get("device"),
                fs=cmd.get("fs"),
                pset_id=cmd.get("pset_id"),
                doppler=bool(cmd.get("doppler", True)),
            )
            await hub.broadcast({"type": "waterfall_config", **hub.audio.waterfall_config()})
        elif action == "stop_rx":
            await asyncio.to_thread(hub.audio.stop_rx)
        elif action == "set_waterfall":
            cfg = await asyncio.to_thread(
                lambda: hub.audio.configure_waterfall(
                    fmin=cmd.get("fmin"), fmax=cmd.get("fmax"), nfft=cmd.get("nfft"),
                    floor_db=cmd.get("floor_db"), ceil_db=cmd.get("ceil_db"),
                )
            )
            await hub.broadcast({"type": "waterfall_config", **cfg})
        elif action == "tx":
            await asyncio.to_thread(
                lambda: hub.audio.transmit(
                    cargo=cmd.get("cargo") or None,
                    app_fields=cmd.get("app_fields") or None,
                    class_id=cmd.get("class_id"),
                    app_type=cmd.get("app_type"),
                    output_device=cmd.get("output_device"),
                    monitor=bool(cmd.get("monitor", True)),
                    loopback=bool(cmd.get("loopback", False)),
                )
            )
        elif action == "set_verify":
            hub.audio.set_verify(bool(cmd.get("verify")))
        elif action == "devices":
            await hub.broadcast({"type": "devices", "devices": list_devices()})
        await hub.broadcast(hub.status())
    except Exception as exc:
        await hub.broadcast({"type": "event", "message": f"ERROR: {exc}"})


def run(host: str = "127.0.0.1", port: int = 8000, engine: JanusEngine | None = None) -> None:
    """Run the web server (blocking) on the given host/port."""
    import uvicorn

    uvicorn.run(create_app(engine), host=host, port=port, log_level="info")
