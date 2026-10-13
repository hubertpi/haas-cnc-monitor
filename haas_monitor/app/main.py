from .ui_settings import UISettings, TitleInput
import asyncio
import contextlib
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from .collector import CollectionError, factory
from .manager import Manager
from .models import Endpoint, MachineInput, ScanInput, BoostInput
from .discovery import Discovery
from .schedule import ScheduleInput
from .mqtt import Publisher
from .store import Store


async def supervisor_mqtt(options):
    if options.get("mqtt_host"):
        return {"host": options["mqtt_host"], "port": options.get("mqtt_port", 1883),
                "username": options.get("mqtt_username"), "password": options.get("mqtt_password"),
                "tls": options.get("mqtt_tls", False)}
    if os.getenv("SUPERVISOR_TOKEN"):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get("http://supervisor/services/mqtt", headers={
                    "Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]})
                response.raise_for_status()
                data = response.json()["data"]
                return {"host": data["host"], "port": data["port"],
                        "username": data.get("username"), "password": data.get("password"),
                        "tls": data.get("ssl", False)}
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    return {}


def create_app(directory=None, options=None, collector_factory=None, publisher=None, development=False):
    directory = Path(directory or os.getenv("MONITOR_DATA", "/data"))
    directory.mkdir(parents=True, exist_ok=True)
    options = options if options is not None else json.loads((directory / "options.json").read_text())
    csrf = secrets.token_urlsafe(32)
    probes = {}
    test_lock = asyncio.Semaphore(2)
    ui_settings = UISettings(directory / "ui-settings.json")
    ui_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        pub = publisher or Publisher(directory, await supervisor_mqtt(options), deferred=True)
        startup_error = None
        try:
            make_collector = collector_factory or factory(options)
        except CollectionError as error:
            startup_error = str(error)
            def make_collector():
                raise CollectionError(startup_error)
        manager = Manager(Store(directory / "machines.json"), pub, make_collector)
        app.state.manager, app.state.publisher, app.state.error = manager, pub, startup_error
        app.state.discovery = Discovery(simulation=options.get("mode","simulation")=="simulation")
        pub.start()
        if not startup_error:
            await manager.start()
        else:
            pub.reconcile(set(manager.records))
            for identity in manager.records:
                manager.status[identity] = {**manager.empty_status(), "error": startup_error}
        async def pump():
            service_retry = time.monotonic() + 30
            stats_due = 0
            while True:
                if not pub.broker.get("host") and time.monotonic() >= service_retry:
                    broker = await supervisor_mqtt(options)
                    if broker:
                        pub.broker = broker
                        pub.start()
                    service_retry = time.monotonic() + 30
                from datetime import datetime, timezone
                for identity, health in manager.status.items():
                    if health["online"] and health["last_success"]:
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(health["last_success"])).total_seconds()
                        if age > max(15, min(manager.records[identity]["interval"],manager.records[identity]["fast_interval"]) * 3):
                            health.update(online=False, error="Data read is stale; awaiting collector recovery")
                            pub.availability(identity, False)
                if time.monotonic()>=stats_due:
                    from .manager import utc
                    for identity,health in manager.status.items():
                        if not health["online"]:
                            now = utc()
                            values = manager.utilization.signals(identity)
                            health["signals"]={n:s for n,s in health["signals"].items() if s.get("group")!="utilization"}
                            for name,signal in values.items():
                                signal.update(last_read=now,expire_after=30)
                                health["signals"][name] = signal
                                pub.state(identity,name,signal,now,options.get("mode","simulation")=="simulation")
                            pub.discovery(manager.records[identity],health["signals"],health["controller"])
                    stats_due = time.monotonic()+5
                for identity,(deadline,interval) in list(manager.boosts.items()):
                    if deadline<=time.monotonic():
                        manager.boost(identity,False)
                        pub.put(pub.machine_root(identity)+"/monitor/state/boost","OFF")
                manager.utilization.maintain(time.time())
                pub.pump()
                await asyncio.sleep(0.2)
        task = asyncio.create_task(pump())
        try:
            yield
        finally:
            await app.state.discovery.close()
            await manager.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            pub.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def ingress(request: Request, call_next):
        # Trust socket peer, never a caller-supplied X-Forwarded-For.
        if not development and request.client.host != "172.30.32.2":
            return JSONResponse({"detail": "Home Assistant Ingress required"}, status_code=403)
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(request.headers.get("x-monitor-csrf", ""), csrf):
            return JSONResponse({"detail": "Refresh the interface before making changes"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'"
        return response

    @app.get("/")
    async def index(request: Request):
        import html
        import re
        base = request.headers.get("x-ingress-path", "")
        if base and not re.fullmatch(r"/api/hassio_ingress/[a-zA-Z0-9_-]+/?", base):
            raise HTTPException(400, "Invalid Ingress path")
        document = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(document.replace("<head>", '<head><base href="' + html.escape(base.rstrip("/") + "/", quote=True) + '">', 1))

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "preferences.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent.parent / "web" / name)

    @app.get("/api/status")
    async def status():
        manager, pub = app.state.manager, app.state.publisher
        return {"csrf": csrf, "mode": options.get("mode", "simulation"),
                "hardware_testing_enabled": options.get("hardware_testing_enabled", False),
                "mqtt_connected": pub.connected, "mqtt_error": pub.error,
                "startup_error": app.state.error, "machines": manager.list(), "discovery": app.state.discovery.view(manager.records.values()), "schedule": manager.schedule.view(), "ui_settings": ui_settings.view()}

    @app.put("/api/ui-settings")
    async def save_ui_settings(value: TitleInput):
        async with ui_lock:
            try: return ui_settings.save(value)
            except ValueError as error: raise HTTPException(409, str(error)) from error

    @app.put("/api/schedule")
    async def save_schedule(value: ScheduleInput):
        try: return await app.state.manager.save_schedule(value)
        except ValueError as error: raise HTTPException(409,str(error)) from error

    @app.post("/api/discovery")
    async def scan(value: ScanInput):
        if app.state.error: raise HTTPException(409,app.state.error)
        try:
            app.state.discovery.start(value.network,value.port,app.state.manager.records.values())
        except ValueError as error:
            raise HTTPException(409,str(error)) from error
        return app.state.discovery.view(app.state.manager.records.values())

    @app.delete("/api/discovery")
    async def cancel_scan():
        await app.state.discovery.close()
        return app.state.discovery.view(app.state.manager.records.values())

    @app.post("/api/machines/{identity}/boost")
    async def boost(identity: uuid.UUID, value: BoostInput):
        try:
            app.state.manager.boost(str(identity),value.enabled,value.minutes,value.interval)
        except KeyError:
            raise HTTPException(404,"Machine no longer exists")
        except ValueError as error:
            raise HTTPException(409,str(error)) from error
        return {"ok":True}

    @app.post("/api/test")
    async def test(endpoint: Endpoint):
        if test_lock.locked():
            raise HTTPException(429, "Two connection tests are already running")
        if app.state.error:
            raise HTTPException(409, app.state.error)
        async with test_lock:
            collector = (collector_factory or factory(options))()
            try:
                result = await collector.read(endpoint.model_dump())
                controller = result["controller"]
                token = secrets.token_urlsafe(32)
                # Tests expire, and are scoped to the endpoint and timeout tested.
                for old in list(probes):
                    if probes[old][1] < time.monotonic():
                        del probes[old]
                probes[token] = (endpoint.model_dump(), time.monotonic() + 300)
                if len(probes) > 100:
                    probes.pop(next(iter(probes)))
                return {"ok": True, "test_token": token, "controller": controller,
                        "suggested_name": ("SIM · " if result.get("simulation") else "") + controller.get("model", "Haas CNC") + " · " + endpoint.host,
                        "values_collected": len(result["signals"]), "unsupported": result.get("unsupported", {}),
                        "simulation": result.get("simulation", False)}
            except CollectionError as error:
                return JSONResponse({"ok": False, "error": str(error), "code": error.code}, status_code=422)
            finally:
                await collector.close()

    async def persist(value, request, identity=None):
        if app.state.error:
            raise HTTPException(409, app.state.error)
        endpoint = {k: getattr(value, k) for k in ("host", "port", "timeout", "protocol", "device")}
        old = app.state.manager.records.get(identity)
        changed_endpoint = not old or any(old[k] != endpoint[k] for k in endpoint)
        if changed_endpoint:
            token = request.headers.get("x-connection-test", "")
            probe = probes.get(token)
            if not probe or probe[0] != endpoint or probe[1] < time.monotonic():
                raise HTTPException(409, "Test this exact Haas endpoint successfully before saving")
        try:
            record = await app.state.manager.save(value, identity)
            if changed_endpoint:
                probes.pop(token, None)
            return record
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, "Machine no longer exists") from error

    @app.post("/api/machines", status_code=201)
    async def add(value: MachineInput, request: Request):
        return await persist(value, request)

    @app.put("/api/machines/{identity}")
    async def edit(identity: uuid.UUID, value: MachineInput, request: Request):
        return await persist(value, request, str(identity))

    @app.delete("/api/machines/{identity}")
    async def remove(identity: uuid.UUID):
        try:
            await app.state.manager.remove(str(identity))
        except KeyError as error:
            raise HTTPException(404, "Machine no longer exists") from error
        return {"removed": True, "mqtt_cleanup": "queued; requires broker acknowledgement"}

    @app.get("/api/diagnostics")
    async def diagnostics():
        return {"mode": options.get("mode", "simulation"), "machines": app.state.manager.list(),
                "mqtt_connected": app.state.publisher.connected,
                "pending_mqtt_topics": len(app.state.publisher.dirty), "schedule": app.state.manager.schedule.view(),
                "production_accounting":{identity:app.state.manager.utilization.summary(identity) for identity in app.state.manager.records}}

    return app


if __name__ == "__main__":
    import uvicorn
    development = os.getenv("MONITOR_DEVELOPMENT") == "1"
    uvicorn.run(create_app(development=development), host="127.0.0.1" if development else "0.0.0.0",
                port=8099, proxy_headers=False)
