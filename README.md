# Camera LCD mockup

A small Python window representing the Pi touchscreen LCD resolution (480×320) in a fixed, non-resizable 960×640 window at 2× scale. It shows the live camera preview with a touch-friendly `{quit}` button in the upper-right corner.

The app captures a full-resolution **4056×3040 12-bit Bayer RAW** frame from the IMX477 sensor into a NumPy buffer, alongside a processed 2028×1520 RGB preview stream for display. The display fits the camera’s 4:3 image into the 3:2 window without stretching it.

## Run

```bash
python3 camera_ui.py
```

The latest raw sensor frame is kept in `FRAME_BUFFER`. Code in this process can call `get_latest_raw_frame()` to get a safe copy of the latest NumPy array, its pixel format, and capture metadata. Use `copy=False` only when you will not modify the returned array.

The app also listens on `127.0.0.1:8765` for optional image uploads. From another terminal on the Pi, send a JPEG or PNG like this:

```bash
curl -X POST --data-binary @frame.jpg http://127.0.0.1:8765/frame
```

Uploaded images replace the display preview; the raw camera buffer continues updating. Use `--no-camera` to disable camera capture and show uploads only. To accept uploads from another computer on the local network, start the app with `--host 0.0.0.0` and send to the Pi’s address on port `8765`.

Requires Python 3, Picamera2, NumPy, Pygame, and Pillow.
