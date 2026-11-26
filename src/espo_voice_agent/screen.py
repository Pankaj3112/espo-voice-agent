"""Publish the agent's headless browser into the LiveKit room as a video track.

Chromium's CDP screencast pushes a JPEG whenever the page repaints. We decode
the latest frame and re-send it at a steady rate (WebRTC encoders expect a
continuous stream even when the page is idle).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging

from livekit import rtc
from PIL import Image
from playwright.async_api import Page

log = logging.getLogger("screen")

WIDTH, HEIGHT = 1280, 800


class BrowserStreamer:
    def __init__(self, page: Page, room: rtc.Room, fps: float = 6.0) -> None:
        self.page = page
        self.room = room
        self.fps = fps
        self.source = rtc.VideoSource(WIDTH, HEIGHT)
        self._latest: rtc.VideoFrame | None = None
        self._task: asyncio.Task | None = None
        self._cdp = None

    async def start(self) -> None:
        track = rtc.LocalVideoTrack.create_video_track("agent-browser", self.source)
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_SCREENSHARE)
        await self.room.local_participant.publish_track(track, opts)

        self._cdp = await self.page.context.new_cdp_session(self.page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        await self._cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 70, "maxWidth": WIDTH, "maxHeight": HEIGHT},
        )
        self._task = asyncio.create_task(self._pump())
        log.info("browser stream published")

    def _on_frame(self, params: dict) -> None:
        asyncio.create_task(self._handle(params))

    async def _handle(self, params: dict) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            return
        data = base64.b64decode(params["data"])
        self._latest = await asyncio.to_thread(_decode, data)

    async def _pump(self) -> None:
        interval = 1.0 / self.fps
        while True:
            if self._latest is not None:
                self.source.capture_frame(self._latest)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        try:
            if self._cdp:
                await self._cdp.send("Page.stopScreencast")
        except Exception:
            pass


def _decode(jpeg: bytes) -> rtc.VideoFrame:
    img = Image.open(io.BytesIO(jpeg)).convert("RGBA")
    if img.size != (WIDTH, HEIGHT):
        canvas = Image.new("RGBA", (WIDTH, HEIGHT), (255, 255, 255, 255))
        canvas.paste(img, (0, 0))
        img = canvas
    return rtc.VideoFrame(WIDTH, HEIGHT, rtc.VideoBufferType.RGBA, img.tobytes())
