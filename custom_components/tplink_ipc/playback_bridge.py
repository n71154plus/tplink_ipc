"""Localhost MPEG-TS bridge: Streamd playback → HTTP for go2rtc ffmpeg."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from .stream_session import (
    TPLinkMediaStreamError,
    TPLinkMediaStreamSession,
    explain_stream_error,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8791
# Skip HTTP re-login when job.stok was just refreshed by stream_playback.
STOK_FRESH_S = 90.0
# Abandon prefetch if nobody attaches (WebRTC not opened).
PREFETCH_ORPHAN_S = 75.0
# First-byte wait after go2rtc connects (prefetch usually already warm).
PRIMING_TIMEOUT_S = 6.0
# Empty Streamd window idle before trying next port/path.
STREAM_IDLE_S = 1.2


@dataclass
class _Prefetch:
    """Background Streamd reader started at job register time."""

    queue: asyncio.Queue[bytes | None]
    stop: threading.Event
    first: asyncio.Future[bytes | None]
    thread: threading.Thread | None = None
    attached: bool = False


@dataclass
class PlaybackJob:
    """One active / pending Streamd playback for a go2rtc stream name."""

    stream_name: str
    host: str
    username: str
    password: str
    stok: str
    start_time: str
    end_time: str
    channel: int = 0
    stream_port: int = 8800
    use_stok_path: bool = True
    max_seconds: float = 120.0
    preferred_port: int | None = None
    preferred_use_stok: bool | None = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    created: float = field(default_factory=time.time)
    prefetch: _Prefetch | None = field(default=None, repr=False)


class PlaybackBridge:
    """aiohttp server on 127.0.0.1 serving video/mp2t for go2rtc."""

    def __init__(
        self,
        *,
        bind: str = DEFAULT_BIND,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.bind = bind
        self.port = port
        self._jobs: dict[str, PlaybackJob] = {}
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # host -> last working (port, use_stok)
        self._host_prefs: dict[str, tuple[int, bool]] = {}

    @property
    def base_url(self) -> str:
        return f"http://{self.bind}:{self.port}"

    def mpegts_url(self, stream_name: str, token: str) -> str:
        return f"{self.base_url}/playback/{stream_name}.ts?token={token}"

    def ffmpeg_src(self, stream_name: str, token: str) -> str:
        # go2rtc API rejects exec: ("source from insecure producer").
        # Bridge waits for first MPEG-TS before HTTP 200 so ffmpeg:http works.
        # #timeout keeps producer alive while Streamd primes.
        url = self.mpegts_url(stream_name, token)
        return f"ffmpeg:{url}#video=copy#timeout=20"

    async def async_start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/playback/{name}.ts", self._handle_mpegts)
        app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.bind, self.port)
        await self._site.start()
        _LOGGER.info("TP-Link playback bridge on %s:%s", self.bind, self.port)

    async def async_stop(self) -> None:
        if self._runner is None:
            return
        async with self._lock:
            for job in self._jobs.values():
                self._stop_prefetch(job)
        await self._runner.cleanup()
        self._runner = None
        self._site = None

    def _stop_prefetch(self, job: PlaybackJob) -> None:
        pf = job.prefetch
        if pf is None:
            return
        pf.stop.set()
        if pf.thread is not None and pf.thread.is_alive():
            pf.thread.join(timeout=1.5)
        job.prefetch = None

    async def async_register(self, job: PlaybackJob) -> PlaybackJob:
        """Register job and immediately start Streamd prefetch (warm first byte)."""
        prefs = self._host_prefs.get(job.host)
        if prefs and job.preferred_port is None:
            job.preferred_port, job.preferred_use_stok = prefs
        loop = asyncio.get_running_loop()
        async with self._lock:
            old = self._jobs.get(job.stream_name)
            if old is not None:
                self._stop_prefetch(old)
            self._jobs[job.stream_name] = job
        self._start_prefetch(job, loop)
        return job

    def _start_prefetch(
        self, job: PlaybackJob, loop: asyncio.AbstractEventLoop
    ) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        stop = threading.Event()
        first: asyncio.Future[bytes | None] = loop.create_future()
        pf = _Prefetch(queue=queue, stop=stop, first=first)
        job.prefetch = pf

        def _produce() -> None:
            try:
                age = time.time() - job.created
                if age >= STOK_FRESH_S:
                    try:
                        self._refresh_stok(job)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "bridge %s re-login failed: %s", job.stream_name, err
                        )
                self._stream_job(job, queue, loop, stop, first)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "playback bridge %s failed: %s", job.stream_name, err
                )
                if not first.done():
                    loop.call_soon_threadsafe(first.set_result, None)
            finally:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                except RuntimeError:
                    pass

        worker = threading.Thread(
            target=_produce, name=f"tplink-pb-{job.stream_name}", daemon=True
        )
        pf.thread = worker
        worker.start()

        def _orphan_watch() -> None:
            # If WebRTC never attaches, free Streamd.
            deadline = time.time() + PREFETCH_ORPHAN_S
            while time.time() < deadline:
                if stop.is_set() or pf.attached:
                    return
                time.sleep(0.5)
            if not pf.attached and not stop.is_set():
                _LOGGER.debug(
                    "prefetch orphan stop %s (no go2rtc attach)", job.stream_name
                )
                stop.set()

        threading.Thread(
            target=_orphan_watch,
            name=f"tplink-pb-orphan-{job.stream_name}",
            daemon=True,
        ).start()

    async def async_get(self, stream_name: str) -> PlaybackJob | None:
        async with self._lock:
            return self._jobs.get(stream_name)

    async def _handle_health(self, _request: web.Request) -> web.Response:
        async with self._lock:
            jobs = {
                name: {
                    "token": job.token,
                    "host": job.host,
                    "start_time": job.start_time,
                    "end_time": job.end_time,
                    "age_s": round(time.time() - job.created, 1),
                    "prefetch": job.prefetch is not None,
                    "first_ready": bool(
                        job.prefetch
                        and job.prefetch.first.done()
                        and job.prefetch.first.exception() is None
                        and bool(job.prefetch.first.result())
                    ),
                }
                for name, job in self._jobs.items()
            }
        return web.json_response({"ok": True, "jobs": jobs})

    def _refresh_stok(self, job: PlaybackJob) -> str:
        """Login again — stok in the job may be minutes old when go2rtc connects."""
        import json
        import ssl
        import urllib.request

        from .stream_session import tp_encrypt

        enc = tp_encrypt(job.password)
        body = json.dumps(
            {
                "method": "do",
                "login": {"username": job.username, "password": enc},
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{job.host}/",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=6, context=ctx) as r:
            data = json.loads(r.read().decode())
        stok = data.get("stok")
        if not stok:
            raise TPLinkMediaStreamError(f"login failed: {data}")
        job.stok = str(stok)
        return job.stok

    async def _handle_mpegts(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        token = request.query.get("token", "")
        job = await self.async_get(name)
        if job is None or token != job.token:
            raise web.HTTPNotFound(text="unknown or expired playback session")

        loop = asyncio.get_running_loop()
        pf = job.prefetch
        if pf is None or pf.stop.is_set():
            # Prefetch died / never started — cold start.
            self._start_prefetch(job, loop)
            pf = job.prefetch
        assert pf is not None
        pf.attached = True

        # Wait for first MPEG-TS before HTTP 200 so ffmpeg probe succeeds.
        # Prefetch usually already has the first packet from register time.
        try:
            priming = await asyncio.wait_for(asyncio.shield(pf.first), timeout=PRIMING_TIMEOUT_S)
        except asyncio.TimeoutError:
            pf.stop.set()
            if pf.thread is not None:
                pf.thread.join(timeout=2.0)
            raise web.HTTPBadGateway(text="no MPEG-TS from camera (timeout)")
        if not priming:
            pf.stop.set()
            if pf.thread is not None:
                pf.thread.join(timeout=2.0)
            raise web.HTTPBadGateway(
                text="no MPEG-TS from camera (empty slot or Streamd idle)"
            )

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "video/mp2t",
                "Cache-Control": "no-store",
                "Connection": "close",
            },
        )
        await resp.prepare(request)
        await resp.write(priming)
        try:
            while True:
                chunk = await pf.queue.get()
                if chunk is None:
                    break
                await resp.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pf.stop.set()
        finally:
            pf.stop.set()
            if pf.thread is not None:
                pf.thread.join(timeout=2.0)
        return resp

    def _stream_job(
        self,
        job: PlaybackJob,
        queue: asyncio.Queue[bytes | None],
        loop: asyncio.AbstractEventLoop,
        stop: threading.Event,
        first: asyncio.Future[bytes | None] | None = None,
    ) -> None:
        got_first = False

        def _put(data: bytes | None) -> None:
            nonlocal got_first
            if stop.is_set():
                return
            if data and not got_first and first is not None and not first.done():
                got_first = True
                loop.call_soon_threadsafe(first.set_result, data)
                return
            # Block until consumer drains (or orphan stop) — do not abort after 15s
            # or prefetch dies before the user opens WebRTC.
            try:
                fut = asyncio.run_coroutine_threadsafe(queue.put(data), loop)
                while not stop.is_set():
                    try:
                        fut.result(timeout=1.0)
                        return
                    except TimeoutError:
                        continue
                fut.cancel()
            except Exception:  # noqa: BLE001
                stop.set()

        ports: list[int] = []
        if job.preferred_port:
            ports.append(int(job.preferred_port))
        for p in (job.stream_port, 8800, 8000):
            if p and int(p) not in ports:
                ports.append(int(p))

        stok_opts: list[bool] = []
        if job.preferred_use_stok is not None:
            stok_opts.append(bool(job.preferred_use_stok))
        for v in (True, False):
            if v not in stok_opts:
                stok_opts.append(v)

        last_err: Exception | None = None
        for port in ports:
            for use_stok in stok_opts:
                if stop.is_set():
                    return
                try:
                    self._stream_once(job, port, use_stok, _put, stop)
                    self._host_prefs[job.host] = (port, use_stok)
                    job.preferred_port = port
                    job.preferred_use_stok = use_stok
                    if not got_first and first is not None and not first.done():
                        loop.call_soon_threadsafe(first.set_result, None)
                    return
                except (TPLinkMediaStreamError, OSError, TimeoutError) as err:
                    last_err = err
                    _LOGGER.warning(
                        "bridge try %s:%s stok=%s: %s",
                        job.host,
                        port,
                        use_stok,
                        err,
                    )
                    # Connection refused → don't also try use_stok=False on same port.
                    err_s = str(err).lower()
                    if isinstance(err, ConnectionRefusedError) or (
                        isinstance(err, OSError)
                        and (
                            getattr(err, "errno", None) in (111, 61)
                            or "refused" in err_s
                        )
                    ):
                        break
        if first is not None and not first.done():
            loop.call_soon_threadsafe(first.set_result, None)
        if last_err:
            raise last_err

    def _stream_once(
        self,
        job: PlaybackJob,
        port: int,
        use_stok: bool,
        put,
        stop: threading.Event,
    ) -> None:
        with TPLinkMediaStreamSession(
            job.host,
            job.username,
            job.password,
            job.stok,
            stream_port=port,
            use_stok_path=use_stok,
            connect_timeout=3.0,
        ) as session:
            try:
                session.send_json({"type": "request", "seq": 0, "params": {}})
            except Exception:  # noqa: BLE001
                pass
            resp = session.request_playback(
                job.start_time,
                job.end_time,
                channel=job.channel,
                client_id=2,
            )
            err = (resp.get("params") or {}).get("error_code", 0)
            if err not in (0, None):
                hint = explain_stream_error(err) or ""
                raise TPLinkMediaStreamError(
                    f"playback failed ({err}) {hint}: {resp}"
                )

            started = time.time()
            last_data = started
            idle = STREAM_IDLE_S
            got_any = False
            for ctype, _headers, body in session.iter_parts(idle_timeout=idle):
                if stop.is_set():
                    break
                if "mp2t" in ctype and body:
                    put(body)
                    got_any = True
                    last_data = time.time()
                if time.time() - started >= job.max_seconds:
                    break
                if time.time() - last_data > idle and last_data > started:
                    break
            if not got_any:
                raise TPLinkMediaStreamError(
                    f"playback ok but no MPEG-TS "
                    f"(start={job.start_time} end={job.end_time}) — "
                    f"try a completed hour slot"
                )
