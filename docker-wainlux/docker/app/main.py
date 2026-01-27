"""
Flask app for K6 laser control

Web interface for Wainlux K6 laser engraver.
Access at http://<pi-ip>:8080
"""

from flask import Flask, render_template, request, jsonify, Response
from pathlib import Path
import logging
import tempfile
import sys
from datetime import datetime
import json
import time
import queue
import threading

# Add k6 library to path (located in /app/k6 in container)
# From /app/app/main.py, parent is /app/app, parent.parent is /app
sys.path.insert(0, str(Path(__file__).parent.parent))

from k6.driver import WainluxK6
from k6.transport import SerialTransport
from k6.csv_logger import CSVLogger
from k6 import protocol

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="../templates", static_folder="../static")

# K6 driver and transport
k6_driver = WainluxK6()
k6_transport = None
k6_connected = False
k6_version = None  # Store firmware version string

# Progress tracking for SSE
progress_queue = queue.Queue()
burn_in_progress = False
burn_cancel_event = threading.Event()  # Signal to cancel active burn

# Data directory for logs
DATA_DIR = Path(__file__).parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "gif"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def emit_progress(phase: str, progress: int, message: str):
    """Emit a progress event to all SSE listeners."""
    event = {
        "phase": phase,
        "progress": progress,
        "message": message,
        "timestamp": datetime.now().isoformat(),
    }
    logger.info(f"EMIT_PROGRESS: phase={phase}, progress={progress}, message={message}")
    try:
        progress_queue.put_nowait(event)
        logger.info("EMIT_PROGRESS: queued successfully")
    except queue.Full:
        logger.warning("EMIT_PROGRESS: queue full, dropped event")
        pass  # Drop events if queue is full


def color_aware_grayscale(img, laser_color="blue"):
    """Convert RGB image to grayscale optimized for laser wavelength.

    Args:
        img: PIL Image in RGB mode
        laser_color: 'blue', 'red', 'green', or 'neutral'

    Returns:
        PIL Image in 'L' mode (grayscale)

    Color wavelengths absorb differently:
    - Blue laser (450nm): Absorbs red+green, reflects blue
    - Red laser (650nm): Absorbs blue+green, reflects red
    - Green laser (532nm): Absorbs red+blue, reflects green

    For best engraving contrast, emphasize colors the laser absorbs.
    """
    import numpy as np

    if img.mode != "RGB":
        img = img.convert("RGB")

    arr = np.array(img, dtype=np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    if laser_color == "blue":
        # Blue laser: emphasize R+G channels (absorb), de-emphasize B (reflects)
        gray = 0.45 * r + 0.45 * g + 0.10 * b
    elif laser_color == "red":
        # Red laser: emphasize B+G channels, de-emphasize R
        gray = 0.10 * r + 0.45 * g + 0.45 * b
    elif laser_color == "green":
        # Green laser: emphasize R+B channels, de-emphasize G
        gray = 0.45 * r + 0.10 * g + 0.45 * b
    else:  # neutral
        # Standard luminance conversion
        gray = 0.299 * r + 0.587 * g + 0.114 * b

    gray = np.clip(gray, 0, 255).astype(np.uint8)
    from PIL import Image

    return Image.fromarray(gray, mode="L")


@app.route("/")
def index():
    return render_template("index.html", connected=k6_connected)


@app.route("/calibration")
def calibration():
    """Advanced calibration and testing page"""
    return render_template("calibration.html", connected=k6_connected)


@app.route("/api/connect", methods=["POST"])
def connect():
    global k6_transport, k6_connected, k6_version

    try:
        # Create transport and connect
        k6_transport = SerialTransport(
            port="/dev/ttyUSB0", baudrate=115200, timeout=2.0
        )
        success, version = k6_driver.connect_transport(k6_transport)
        k6_connected = True
        k6_version = f"v{version[0]}.{version[1]}.{version[2]}"
        logger.info(f"Connected to K6 {k6_version}")
        return jsonify({"success": True, "connected": True, "version": k6_version})
    except Exception as e:
        logger.error(f"Connect failed: {e}")
        k6_connected = False
        k6_transport = None
        return jsonify({"success": False, "connected": False, "error": str(e)}), 500


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    global k6_transport, k6_connected, k6_version

    if k6_transport:
        try:
            k6_transport.close()
        except Exception as e:
            logger.warning(f"Disconnect warning: {e}")

    k6_transport = None
    k6_connected = False
    k6_version = None
    logger.info("Disconnected from K6")
    return jsonify({"success": True, "connected": False})


@app.route("/api/test/bounds", methods=["POST"])
def test_bounds():
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        # Use transport-based method
        success = k6_driver.draw_bounds_transport(k6_transport)
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Bounds test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/test/home", methods=["POST"])
def test_home():
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        # Send FRAMING (0x21) to stop preview mode started by BOUNDS (0x20)
        # Note: This returns laser to CENTER position from BOUNDS, not origin
        protocol.send_cmd_checked(
            k6_transport,
            "FRAMING (stop preview)",
            bytes([0x21, 0x00, 0x04, 0x00]),
            timeout=2.0,
            expect_ack=True,
        )

        # Then send HOME to return to origin (0,0)
        protocol.send_cmd_checked(
            k6_transport,
            "HOME",
            bytes([0x17, 0x00, 0x04, 0x00]),
            timeout=10.0,
            expect_ack=True,
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Home test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/jog", methods=["POST"])
def jog():
    """Move laser head to absolute position using BOUNDS positioning"""
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        center_x = int(data.get("x", 800))
        center_y = int(data.get("y", 800))
        disable_limits = data.get("disable_limits", False)

        # Clamp to work area (0-1600) unless limits disabled
        if not disable_limits:
            center_x = max(0, min(1600, center_x))
            center_y = max(0, min(1600, center_y))
        else:
            # Still apply reasonable max to prevent overflow (hardware max unknown)
            center_x = max(-200, min(2000, center_x))
            center_y = max(-200, min(2000, center_y))

        # Use BOUNDS command to move laser to position
        # Small bounds (1x1px) effectively positions the laser
        success = k6_driver.draw_bounds_transport(
            k6_transport, width=1, height=1, center_x=center_x, center_y=center_y
        )

        if success:
            return jsonify(
                {
                    "success": True,
                    "position": {"x": center_x, "y": center_y},
                    "limits_disabled": disable_limits,
                }
            )
        else:
            return jsonify({"success": False, "error": "BOUNDS command failed"}), 500

    except Exception as e:
        logger.error(f"Jog failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mark", methods=["POST"])
def mark():
    """Burn a single pixel mark at current position"""
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        center_x = int(data.get("x", 800))
        center_y = int(data.get("y", 800))
        power = int(data.get("power", 500))
        depth = int(data.get("depth", 10))

        # Burn a single pixel at position
        success = k6_driver.mark_position_transport(
            k6_transport, center_x=center_x, center_y=center_y, power=power, depth=depth
        )

        if success:
            return jsonify(
                {
                    "success": True,
                    "position": {"x": center_x, "y": center_y},
                    "power": power,
                    "depth": depth,
                    "message": f"Marked position ({center_x}, {center_y})",
                }
            )
        else:
            return jsonify({"success": False, "error": "Mark failed"}), 500

    except Exception as e:
        logger.error(f"Mark failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/crosshair", methods=["POST"])
def crosshair():
    """Toggle crosshair/positioning laser (0x06 ON, 0x07 OFF)"""
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        enable = data.get("enable", False)

        # Send CROSSHAIR ON (0x06) or OFF (0x07)
        opcode = 0x06 if enable else 0x07
        cmd_name = "CROSSHAIR ON" if enable else "CROSSHAIR OFF"

        protocol.send_cmd_checked(
            k6_transport,
            cmd_name,
            bytes([opcode, 0x00, 0x04, 0x00]),
            timeout=2.0,
            expect_ack=True,
        )

        return jsonify(
            {
                "success": True,
                "enabled": enable,
                "message": f"Crosshair {('ON' if enable else 'OFF')}",
            }
        )

    except Exception as e:
        logger.error(f"Crosshair toggle failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/test/stop", methods=["POST"])
def test_stop():
    """Cancel burn and reset device - interrupts Python script and sends CONNECT to reset device"""
    global burn_cancel_event

    # Set cancel flag to interrupt any active burn loop
    burn_cancel_event.set()
    logger.info("Burn cancellation requested")

    # Allow STOP even if not "connected" - best effort
    if k6_transport:
        try:
            # Send STOP command first
            k6_transport.write(bytes([0x16, 0x00, 0x04, 0x00]))
            time.sleep(0.1)

            # Send CONNECT command to reset device state
            protocol.send_cmd_checked(
                k6_transport,
                "CONNECT #1",
                bytes([0x0A, 0x00, 0x04, 0x00]),
                timeout=2.0,
                expect_ack=True,
            )

            logger.info("STOP + CONNECT sent, device reset")
            emit_progress("stopped", 0, "Burn cancelled and device reset")
            return jsonify({"success": True, "message": "Burn cancelled, device reset"})
        except Exception as e:
            logger.warning(f"Stop/reset partial: {e}")
            emit_progress("stopped", 0, "Burn cancelled (device reset may have failed)")
            return jsonify({"success": True, "message": "Cancel signal sent"})
    else:
        emit_progress("stopped", 0, "Burn cancelled")
        return jsonify({"success": True, "message": "Cancel signal sent"})


@app.route("/api/engrave", methods=["POST"])
def engrave():
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    if "image" not in request.files:
        return jsonify({"success": False, "error": "No image"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"success": False, "error": "Invalid file type"}), 400

    try:
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        # Validate image size before processing
        from PIL import Image

        with Image.open(tmp_path) as img:
            width, height = img.size
            # K6 work area: 80x80mm = 1600x1600px @ 0.05mm/px or 1067x1067px @ 0.075mm/px
            # Allow up to 1600x1600 for full resolution testing
            if width > 1600 or height > 1600:
                Path(tmp_path).unlink(missing_ok=True)
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"Image too large: {width}x{height}px (max 1600x1600)",
                        }
                    ),
                    400,
                )

        # Get parameters
        depth = int(request.form.get("depth", 100))
        depth = max(1, min(255, depth))
        power = int(request.form.get("power", 1000))
        power = max(0, min(1000, power))

        # Generate CSV log filename
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        csv_path = DATA_DIR / f"burn-{timestamp}.csv"

        # Engrave with CSV logging
        with CSVLogger(str(csv_path)) as logger:
            result = k6_driver.engrave_transport(
                k6_transport, tmp_path, power=power, depth=depth, csv_logger=logger
            )

        # Clean up temp file
        Path(tmp_path).unlink(missing_ok=True)

        if result["ok"]:
            return jsonify(
                {
                    "success": True,
                    "message": result["message"],
                    "total_time": result["total_time"],
                    "chunks": result["chunks"],
                    "csv_log": str(csv_path),
                }
            )
        else:
            return jsonify({"success": False, "error": result["message"]}), 500

    except Exception as e:
        logger.error(f"Engrave failed: {e}")
        # Clean up temp file on error
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def status():
    """Get current K6 status"""
    response = {
        "connected": k6_connected,
        "state": "idle" if k6_connected else "disconnected",
        "progress": 0,
        "message": "Ready" if k6_connected else "Not connected",
        "max_width": 1600,
        "max_height": 1600,
    }
    if k6_version:
        response["version"] = k6_version
    return jsonify(response)


@app.route("/api/progress/stream")
def progress_stream():
    """SSE endpoint for real-time progress updates."""

    def generate():
        # Send initial connection event
        initial_event = {
            "phase": "connected",
            "progress": 0,
            "message": "Stream connected",
        }
        yield f"data: {json.dumps(initial_event)}\n\n"

        while True:
            try:
                # Wait for events with timeout
                event = progress_queue.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"

                # Check if burn is complete
                if event.get("phase") == "complete" or event.get("phase") == "error":
                    break
            except queue.Empty:
                # Send keepalive
                yield ": keepalive\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/calibration/generate", methods=["POST"])
def calibration_generate():  # noqa: C901
    """Generate calibration test pattern"""
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        resolution = float(data.get("resolution", 0.05))
        pattern = data.get("pattern", "center")  # center, corners, frame, grid
        size_mm = float(data.get("size_mm", 10.0))

        from PIL import Image, ImageDraw

        def mm_to_px(mm: float) -> int:
            return int(round(mm / resolution))

        # Generate pattern based on type
        if pattern == "center":
            size_px = mm_to_px(size_mm)
            img = Image.new("1", (size_px, size_px), 1)
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, size_px - 1, size_px - 1), outline=0, width=2)
            # Crosshairs
            center = size_px // 2
            cross_half = mm_to_px(2.5)
            draw.line(
                (center - cross_half, center, center + cross_half, center),
                fill=0,
                width=1,
            )
            draw.line(
                (center, center - cross_half, center, center + cross_half),
                fill=0,
                width=1,
            )

        elif pattern == "corners":
            work_area_px = mm_to_px(80.0)
            box_px = mm_to_px(size_mm)
            img = Image.new("1", (work_area_px, work_area_px), 1)

            # Create single box
            box = Image.new("1", (box_px, box_px), 1)
            draw_box = ImageDraw.Draw(box)
            draw_box.rectangle((0, 0, box_px - 1, box_px - 1), outline=0, width=2)

            # Place in corners
            positions = [
                (0, 0),
                (work_area_px - box_px, 0),
                (0, work_area_px - box_px),
                (work_area_px - box_px, work_area_px - box_px),
            ]
            for x, y in positions:
                img.paste(box, (x, y))

            # Border
            draw = ImageDraw.Draw(img)
            draw.rectangle(
                (0, 0, work_area_px - 1, work_area_px - 1), outline=0, width=1
            )

        elif pattern == "frame":
            size_px = mm_to_px(80.0)
            img = Image.new("1", (size_px, size_px), 1)
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, size_px - 1, size_px - 1), outline=0, width=2)

        elif pattern == "grid":
            # Graph paper pattern - configurable grid spacing covering work area
            work_area_px = mm_to_px(80.0)
            grid_spacing_px = mm_to_px(size_mm)

            img = Image.new("1", (work_area_px, work_area_px), 1)
            draw = ImageDraw.Draw(img)

            # Draw vertical lines
            x = 0
            while x <= work_area_px:
                draw.line((x, 0, x, work_area_px - 1), fill=0, width=1)
                x += grid_spacing_px

            # Draw horizontal lines
            y = 0
            while y <= work_area_px:
                draw.line((0, y, work_area_px - 1, y), fill=0, width=1)
                y += grid_spacing_px

            # Draw border thicker
            draw.rectangle(
                (0, 0, work_area_px - 1, work_area_px - 1), outline=0, width=2
            )

        else:
            return jsonify({"success": False, "error": "Invalid pattern"}), 400

        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name)
            tmp_path = tmp.name

        return jsonify(
            {
                "success": True,
                "temp_path": tmp_path,
                "width": img.width,
                "height": img.height,
                "pattern": pattern,
                "resolution": resolution,
                "size_mm": size_mm,
            }
        )

    except Exception as e:
        logger.error(f"Calibration generate failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/preview", methods=["POST"])
def calibration_preview():
    """Draw preview bounds using fast 0x20 BOUNDS command"""
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        width = int(data.get("width", 1600))
        height = int(data.get("height", 1600))

        # Validate dimensions
        width = max(1, min(1600, width))
        height = max(1, min(1600, height))

        # Use fast BOUNDS command (0x20)
        success = k6_driver.draw_bounds_transport(
            k6_transport, width=width, height=height
        )

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": f"Preview bounds drawn: {width}×{height}px",
                    "width": width,
                    "height": height,
                }
            )
        else:
            return jsonify({"success": False, "error": "BOUNDS command failed"}), 500

    except Exception as e:
        logger.error(f"Calibration preview failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/burn-bounds", methods=["POST"])
def calibration_burn_bounds():
    """Burn bounds frame - creates physical positioning guide"""
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        from PIL import Image, ImageDraw
        import tempfile
        import time

        data = request.get_json()
        width = int(data.get("width", 1600))
        height = int(data.get("height", 1600))
        power = int(data.get("power", 500))
        depth = int(data.get("depth", 10))

        # Validate
        width = max(1, min(1600, width))
        height = max(1, min(1600, height))
        power = max(0, min(1000, power))
        depth = max(1, min(255, depth))

        # Create rectangle frame image (white background, black outline)
        img = Image.new("1", (width, height), 1)  # 1=white
        draw = ImageDraw.Draw(img)

        # Draw thick border (2-3 pixel width for visibility)
        margin = 2
        draw.rectangle(
            (margin, margin, width - 1 - margin, height - 1 - margin),
            outline=0,  # 0=black
            width=3,
        )

        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name)
            tmp_path = tmp.name

        try:
            start_time = time.time()

            # Burn the frame
            result = k6_driver.engrave_transport(
                k6_transport, tmp_path, power=power, depth=depth
            )

            elapsed = time.time() - start_time

            if result["ok"]:
                return jsonify(
                    {
                        "success": True,
                        "message": "Bounds frame burned",
                        "width": width,
                        "height": height,
                        "power": power,
                        "depth": depth,
                        "total_time": elapsed,
                        "chunks": result["chunks"],
                    }
                )
            else:
                return jsonify({"success": False, "error": result["message"]}), 500
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        logger.error(f"Burn bounds failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/burn", methods=["POST"])
def calibration_burn():  # noqa: C901
    """Burn calibration pattern"""
    if not k6_connected or not k6_transport:
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        # Clear cancel flag at start of new burn
        global burn_cancel_event
        burn_cancel_event.clear()

        # Re-home the device before starting new burn (in case STOP was pressed)
        try:
            protocol.send_cmd_checked(
                k6_transport,
                "HOME",
                bytes([0x17, 0x00, 0x04, 0x00]),
                timeout=10.0,
                expect_ack=True,
            )
        except Exception as e:
            logger.warning(f"Pre-burn HOME failed: {e}")

        data = request.get_json()
        resolution = float(data.get("resolution", 0.05))
        pattern = data.get("pattern", "center")
        size_mm = float(data.get("size_mm", 10.0))
        power = int(data.get("power", 500))
        depth = int(data.get("depth", 10))

        # Validate
        power = max(0, min(1000, power))
        depth = max(1, min(255, depth))

        from PIL import Image, ImageDraw

        def mm_to_px(mm: float) -> int:
            return int(round(mm / resolution))

        # Generate pattern
        if pattern == "center":
            size_px = mm_to_px(size_mm)
            work_area_px = mm_to_px(80.0)

            # Create small box
            box = Image.new("1", (size_px, size_px), 1)
            draw_box = ImageDraw.Draw(box)
            draw_box.rectangle((0, 0, size_px - 1, size_px - 1), outline=0, width=2)
            center = size_px // 2
            cross_half = mm_to_px(2.5)
            draw_box.line(
                (center - cross_half, center, center + cross_half, center),
                fill=0,
                width=1,
            )
            draw_box.line(
                (center, center - cross_half, center, center + cross_half),
                fill=0,
                width=1,
            )

            # Center it in full work area
            img = Image.new("1", (work_area_px, work_area_px), 1)
            offset = (work_area_px - size_px) // 2
            img.paste(box, (offset, offset))

        elif pattern == "corners":
            work_area_px = mm_to_px(80.0)
            box_px = mm_to_px(size_mm)
            img = Image.new("1", (work_area_px, work_area_px), 1)
            box = Image.new("1", (box_px, box_px), 1)
            draw_box = ImageDraw.Draw(box)
            draw_box.rectangle((0, 0, box_px - 1, box_px - 1), outline=0, width=2)
            positions = [
                (0, 0),
                (work_area_px - box_px, 0),
                (0, work_area_px - box_px),
                (work_area_px - box_px, work_area_px - box_px),
            ]
            for x, y in positions:
                img.paste(box, (x, y))
            draw = ImageDraw.Draw(img)
            draw.rectangle(
                (0, 0, work_area_px - 1, work_area_px - 1), outline=0, width=1
            )

        elif pattern == "frame":
            size_px = mm_to_px(80.0)
            img = Image.new("1", (size_px, size_px), 1)
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, size_px - 1, size_px - 1), outline=0, width=2)

        elif pattern == "grid":
            # Graph paper pattern - configurable grid spacing covering work area
            work_area_px = mm_to_px(80.0)
            grid_spacing_px = mm_to_px(size_mm)

            img = Image.new("1", (work_area_px, work_area_px), 1)
            draw = ImageDraw.Draw(img)

            # Draw vertical lines
            x = 0
            while x <= work_area_px:
                draw.line((x, 0, x, work_area_px - 1), fill=0, width=1)
                x += grid_spacing_px

            # Draw horizontal lines
            y = 0
            while y <= work_area_px:
                draw.line((0, y, work_area_px - 1, y), fill=0, width=1)
                y += grid_spacing_px

            # Draw border thicker
            draw.rectangle(
                (0, 0, work_area_px - 1, work_area_px - 1), outline=0, width=2
            )

        else:
            return jsonify({"success": False, "error": "Invalid pattern"}), 400

        # Save and burn
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name)
            tmp_path = tmp.name

        # Clear progress queue
        while not progress_queue.empty():
            try:
                progress_queue.get_nowait()
            except queue.Empty:
                break

        # Emit start event
        emit_progress("start", 0, f"Starting {pattern} pattern burn")
        emit_progress(
            "prepare",
            10,
            f"Image: {img.width}x{img.height}px, Power: {power}, Depth: {depth}",
        )
        emit_progress(
            "prepare", 50, "Processing image with NumPy (~5-10 sec for 1600x1600)..."
        )

        try:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            csv_path = (
                DATA_DIR / f"calibration-{pattern}-{resolution}mm-{timestamp}.csv"
            )

            # Create a custom CSV logger that emits progress
            # Phase mapping: setup=0-33%, upload=33-66%, burn=66-100%
            class ProgressCSVLogger(CSVLogger):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.total_chunks = 0
                    self.current_chunk = 0
                    self.setup_commands = 0

                def log_operation(
                    self,
                    phase,
                    operation,
                    duration_ms,
                    bytes_transferred=0,
                    status_pct=None,
                    state="ACTIVE",
                    response_type="",
                    retry_count=0,
                    device_state="IDLE",
                ):
                    # Check for cancellation
                    if burn_cancel_event.is_set():
                        logger.info("Burn cancelled by user")
                        emit_progress("stopped", 0, "Burn cancelled by user")
                        raise KeyboardInterrupt("Burn cancelled by user")

                    # Call parent with correct parameters
                    super().log_operation(
                        phase=phase,
                        operation=operation,
                        duration_ms=duration_ms,
                        bytes_transferred=bytes_transferred,
                        status_pct=status_pct,
                        state=state,
                        response_type=response_type,
                        retry_count=retry_count,
                        device_state=device_state,
                    )

                    # Debug logging
                    logger.info(f"CSV log: phase={phase}, operation={operation}")

                    # Phase-based progress: setup(0-100%), upload(0-100%), burn(0-100%)
                    if phase == "connect" or phase == "setup":
                        # Setup phase: each command contributes to 0-100%
                        self.setup_commands += 1
                        # FRAMING, JOB_HEADER, CONNECT x2 = ~4 commands → 25% each
                        progress = min(int(self.setup_commands * 25), 100)
                        emit_progress("setup", progress, f"{operation}")

                    elif phase == "burn":
                        # Upload phase: 0-100% for upload bar
                        if "chunk" in operation.lower() and "/" in operation:
                            # Parse "DATA chunk X/Y (line N)"
                            try:
                                for part in operation.split():
                                    if "/" in part:
                                        current, total = part.split("/")
                                        self.current_chunk = int(current)
                                        self.total_chunks = int(total)
                                        # Upload bar shows 0-100% of chunks
                                        chunk_pct = (
                                            self.current_chunk / self.total_chunks
                                        )
                                        progress = int(chunk_pct * 100)
                                        emit_progress(
                                            "upload",
                                            progress,
                                            f"Chunk {current}/{total}",
                                        )
                                        break
                            except (ValueError, IndexError) as e:
                                logger.error(
                                    f"Chunk parse error: {e}, operation={operation}"
                                )
                                emit_progress("upload", 50, f"{operation}")
                        else:
                            emit_progress("upload", 50, f"{operation}")

                    elif phase == "wait" or phase == "finalize":
                        # Burn/wait phase: 0-100% for burn bar
                        if status_pct is not None:
                            # Use device status percentage if available
                            progress = int(status_pct)
                            emit_progress(
                                "burning", progress, f"{operation} ({status_pct}%)"
                            )
                        else:
                            emit_progress("burning", 80, f"{operation}")

            with ProgressCSVLogger(str(csv_path)) as csv_logger:
                emit_progress(
                    "setup", 0, "Image processing complete, starting protocol..."
                )
                result = k6_driver.engrave_transport(
                    k6_transport,
                    tmp_path,
                    power=power,
                    depth=depth,
                    csv_logger=csv_logger,
                )

            Path(tmp_path).unlink(missing_ok=True)

            # Check if cancelled
            if burn_cancel_event.is_set():
                emit_progress("stopped", 0, "Burn cancelled by user")
                return jsonify({"success": False, "error": "Cancelled by user"}), 400

            if result["ok"]:
                emit_progress(
                    "complete", 100, f"Burn complete in {result['total_time']:.1f}s"
                )
                return jsonify(
                    {
                        "success": True,
                        "message": result["message"],
                        "total_time": result["total_time"],
                        "chunks": result["chunks"],
                        "csv_log": str(csv_path),
                        "pattern": pattern,
                        "resolution": resolution,
                        "size_mm": size_mm,
                        "width_px": img.width,
                        "height_px": img.height,
                        "power": power,
                        "depth": depth,
                    }
                )
            else:
                emit_progress("error", 0, result["message"])
                return jsonify({"success": False, "error": result["message"]}), 500

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except KeyboardInterrupt:
        logger.info("Calibration burn cancelled by user")
        emit_progress("stopped", 0, "Burn cancelled")
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Cancelled by user"}), 400
    except Exception as e:
        logger.error(f"Calibration burn failed: {e}")
        emit_progress("error", 0, str(e))
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
