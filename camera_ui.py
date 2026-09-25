#!/usr/bin/env python3
"""480x320 touchscreen camera preview with a full-resolution raw frame buffer."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import queue
import threading

import numpy as np
import pygame
from PIL import Image, UnidentifiedImageError

LCD_WIDTH = 480
LCD_HEIGHT = 320
WINDOW_SCALE = 2
WIDTH = LCD_WIDTH * WINDOW_SCALE
HEIGHT = LCD_HEIGHT * WINDOW_SCALE
RAW_SIZE = (4056, 3040)
PREVIEW_SIZE = (2028, 1520)
RAW_FORMAT = "SRGGB12_CSI2P"
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


class FrameBuffer:
    """Thread-safe latest-frame buffer. Raw Bayer frames stay available for processing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._raw: np.ndarray | None = None
        self._raw_format: str | None = None
        self._metadata: dict[str, object] = {}
        self._preview: np.ndarray | None = None
        self._sequence = 0
        self._status = "Starting camera..."

    def publish_camera_frame(
        self,
        raw: np.ndarray,
        preview: np.ndarray,
        raw_format: str,
        metadata: dict[str, object],
    ) -> None:
        with self._lock:
            self._raw = raw
            self._raw_format = raw_format
            self._metadata = dict(metadata)
            self._preview = preview
            self._sequence += 1
            self._status = f"Camera live · {RAW_SIZE[0]}×{RAW_SIZE[1]} Bayer raw · {raw_format}"

    def publish_uploaded_preview(self, preview: np.ndarray) -> None:
        with self._lock:
            self._preview = preview
            self._sequence += 1
            self._status = "Showing uploaded image"

    def preview_snapshot(self) -> tuple[int, np.ndarray | None, str]:
        with self._lock:
            return self._sequence, self._preview, self._status

    def latest_raw_frame(
        self, copy: bool = True
    ) -> tuple[np.ndarray | None, str | None, dict[str, object]]:
        """Return (Bayer array, pixel format, camera metadata); copy before processing."""
        with self._lock:
            frame = self._raw.copy() if copy and self._raw is not None else self._raw
            return frame, self._raw_format, dict(self._metadata)

    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status


FRAME_BUFFER = FrameBuffer()


def get_latest_raw_frame(
    copy: bool = True,
) -> tuple[np.ndarray | None, str | None, dict[str, object]]:
    """Access the latest full-resolution raw Bayer frame from this app process."""
    return FRAME_BUFFER.latest_raw_frame(copy=copy)


class FrameHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/":
            self.send_error(404)
            return
        body = b"Camera LCD is running. POST a JPEG or PNG image to /frame.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/frame":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_error(413, "Image must be between 1 byte and 32 MiB")
            return
        try:
            payload = self.rfile.read(length)
            if len(payload) != length:
                self.send_error(400, "Incomplete image upload")
                return
            with Image.open(BytesIO(payload)) as image:
                if image.format not in ("JPEG", "PNG", "WEBP"):
                    self.send_error(415, "Send a JPEG, PNG, or WEBP image")
                    return
                preview = np.array(image.convert("RGB"), dtype=np.uint8, copy=True)
        except (UnidentifiedImageError, OSError, ValueError):
            self.send_error(400, "Could not decode image")
            return

        FRAME_BUFFER.publish_uploaded_preview(preview)
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def start_receiver(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), FrameHandler)
    thread = threading.Thread(target=server.serve_forever, name="frame-receiver", daemon=True)
    thread.start()
    return server


def camera_capture_worker(stop_event: threading.Event) -> None:
    camera = None
    try:
        from picamera2 import Picamera2

        camera = Picamera2()
        config = camera.create_video_configuration(
            main={"size": PREVIEW_SIZE, "format": "RGB888"},
            raw={"size": RAW_SIZE, "format": RAW_FORMAT},
            buffer_count=2,
            queue=False,
        )
        camera.configure(config)
        camera.start()
        actual_raw_format = str(camera.camera_configuration()["raw"]["format"])
        FRAME_BUFFER.set_status("Camera started; waiting for the first frame...")
        while not stop_event.is_set():
            arrays, metadata = camera.capture_arrays(["main", "raw"])
            preview, raw = arrays
            FRAME_BUFFER.publish_camera_frame(raw, preview, actual_raw_format, metadata)
    except Exception as exc:
        FRAME_BUFFER.set_status(f"Camera unavailable: {type(exc).__name__}: {exc}")
    finally:
        if camera is not None:
            try:
                camera.stop()
            except Exception:
                pass
            camera.close()


def draw_button(screen: pygame.Surface, font: pygame.font.Font, rect: pygame.Rect) -> None:
    panel = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(panel, (25, 30, 38, 220), panel.get_rect(), border_radius=12)
    pygame.draw.rect(panel, (240, 244, 248, 255), panel.get_rect(), width=2, border_radius=12)
    label = font.render("{quit}", True, (255, 255, 255))
    panel.blit(label, label.get_rect(center=panel.get_rect().center))
    screen.blit(panel, rect)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="upload listener address (default: localhost)")
    parser.add_argument("--port", type=int, default=8765, help="upload listener port")
    parser.add_argument("--no-camera", action="store_true", help="show uploaded images only")
    args = parser.parse_args()

    server = start_receiver(args.host, args.port)
    stop_camera = threading.Event()
    camera_thread = None
    if not args.no_camera:
        camera_thread = threading.Thread(
            target=camera_capture_worker, args=(stop_camera,), name="camera-capture", daemon=True
        )
        camera_thread.start()

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Camera LCD Mockup — 480×320 @ 2x (960×640)")
    clock = pygame.time.Clock()
    button_font = pygame.font.Font(None, 60)
    status_font = pygame.font.Font(None, 36)
    quit_rect = pygame.Rect(0, 0, 264, 112)
    quit_rect.topright = (WIDTH - 24, 24)
    preview_surface: pygame.Surface | None = None
    preview_sequence = -1
    running = True

    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if quit_rect.collidepoint(event.pos):
                        running = False
                elif event.type == pygame.FINGERDOWN:
                    point = (int(event.x * WIDTH), int(event.y * HEIGHT))
                    if quit_rect.collidepoint(point):
                        running = False

            sequence, frame, status = FRAME_BUFFER.preview_snapshot()
            if frame is not None and sequence != preview_sequence:
                image = Image.fromarray(frame)
                image.thumbnail((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
                preview_surface = pygame.image.frombytes(image.tobytes(), image.size, "RGB")
                preview_sequence = sequence

            screen.fill((24, 28, 34))
            if preview_surface is not None:
                screen.blit(preview_surface, preview_surface.get_rect(center=(WIDTH // 2, HEIGHT // 2)))
            else:
                status_surface = status_font.render(status[:52], True, (210, 220, 230))
                screen.blit(status_surface, status_surface.get_rect(center=(WIDTH // 2, HEIGHT // 2)))
            draw_button(screen, button_font, quit_rect)
            pygame.display.flip()
            clock.tick(60)
    finally:
        stop_camera.set()
        server.shutdown()
        server.server_close()
        if camera_thread is not None:
            camera_thread.join(timeout=5)
        pygame.quit()


if __name__ == "__main__":
    main()
