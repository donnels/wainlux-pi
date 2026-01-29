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

from k6.csv_logger import CSVLogger
from services import ImageService, K6Service, QRService
from services.k6_service import K6DeviceManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="../templates", static_folder="../static")

# Device manager and services
device_manager = K6DeviceManager()
k6_service = K6Service(device_manager)
image_service = ImageService()
qr_service = QRService()

# Progress tracking for SSE
progress_queue = queue.Queue()
burn_in_progress = False
burn_cancel_event = threading.Event()  # Signal to cancel active burn

# Data directory for logs
DATA_DIR = Path(__file__).parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True)

# K6 hardware constants
K6_MAX_WIDTH = 1600  # px (80mm at 0.05mm/px resolution)
K6_MAX_HEIGHT = 1520  # px (76mm actual Y-axis measured)
K6_CENTER_X_OFFSET = 67  # px offset for centered positioning
K6_CENTER_Y = 760  # px (middle of 1520px work area)
K6_DEFAULT_CENTER_X = 800  # px default position
K6_DEFAULT_CENTER_Y = 800  # px default position

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "gif"}


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


class ProgressCSVLogger(CSVLogger):
    """CSV logger that emits SSE progress events during burn operations.
    
    Tracks burn phases and emits progress updates via emit_progress():
    - setup/connect: Initial setup commands (0-100%)
    - upload: Data chunk transmission (0-100%)
    - burning: Laser burn progress from device status (0-100%)
    
    Also checks burn_cancel_event to allow user cancellation.
    """
    
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
        # Check for user cancellation
        if burn_cancel_event.is_set():
            logger.info("Burn cancelled by user")
            emit_progress("stopped", 0, "Burn cancelled by user")
            raise KeyboardInterrupt("Burn cancelled by user")

        # Call parent logger
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

        # Emit progress based on phase
        if phase == "connect" or phase == "setup":
            # Setup phase: FRAMING, JOB_HEADER, CONNECT x2 = ~4 commands
            self.setup_commands += 1
            progress = min(int(self.setup_commands * 25), 100)
            emit_progress("setup", progress, f"{operation}")

        elif phase == "burn":
            # Upload phase: track chunk progress
            if "chunk" in operation.lower() and "/" in operation:
                try:
                    for part in operation.split():
                        if "/" in part:
                            current, total = part.split("/")
                            self.current_chunk = int(current)
                            self.total_chunks = int(total)
                            chunk_pct = self.current_chunk / self.total_chunks
                            progress = int(chunk_pct * 100)
                            emit_progress("upload", progress, f"Chunk {current}/{total}")
                            break
                except (ValueError, IndexError) as e:
                    logger.error(f"Chunk parse error: {e}, operation={operation}")
                    emit_progress("upload", 50, f"{operation}")
            else:
                emit_progress("upload", 50, f"{operation}")

        elif phase == "wait" or phase == "finalize":
            # Burning phase: use device status if available
            if status_pct is not None:
                progress = int(status_pct)
                emit_progress("burning", progress, f"{operation} ({status_pct}%)")
            else:
                emit_progress("burning", 80, f"{operation}")


@app.route("/")
def index():
    return render_template("index.html", connected=device_manager.is_connected())


@app.route("/calibration")
def calibration():
    """Advanced calibration and testing page"""
    return render_template("calibration.html", connected=device_manager.is_connected())


@app.route("/qr")
def qr_page():
    """WiFi QR code generation and burn page"""
    return render_template("qr.html", connected=device_manager.is_connected())


@app.route("/api/connect", methods=["POST"])
def connect():
    try:
        success, version = device_manager.connect(
            port="/dev/ttyUSB0", baudrate=115200, timeout=2.0
        )
        if success:
            version_str = f"v{version[0]}.{version[1]}.{version[2]}"
            logger.info(f"Connected to K6 {version_str}")
            return jsonify({"success": True, "connected": True, "version": version_str})
        else:
            return jsonify({"success": False, "connected": False, "error": "Connection failed"}), 500
    except Exception as e:
        logger.error(f"Connect failed: {e}")
        return jsonify({"success": False, "connected": False, "error": str(e)}), 500


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    try:
        device_manager.disconnect()
        logger.info("Disconnected from K6")
        return jsonify({"success": True, "connected": False})
    except Exception as e:
        logger.warning(f"Disconnect warning: {e}")
        return jsonify({"success": True, "connected": False})


@app.route("/api/test/bounds", methods=["POST"])
def test_bounds():
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        success = k6_service.draw_bounds()
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Bounds test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/test/home", methods=["POST"])
def test_home():
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        success = k6_service.home()
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Home test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/jog", methods=["POST"])
def jog():
    """Move laser head to absolute position using BOUNDS positioning"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        center_x = int(data.get("x", 800))
        center_y = int(data.get("y", 800))
        disable_limits = data.get("disable_limits", False)

        success, result = k6_service.jog(center_x, center_y, disable_limits)

        if success:
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": "BOUNDS command failed"}), 500

    except Exception as e:
        logger.error(f"Jog failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mark", methods=["POST"])
def mark():
    """Burn a single pixel mark at current position"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        center_x = int(data.get("x", K6_DEFAULT_CENTER_X))
        center_y = int(data.get("y", K6_DEFAULT_CENTER_Y))
        power = int(data.get("power", 500))
        depth = int(data.get("depth", 10))

        success, result = k6_service.mark(center_x, center_y, power, depth)

        if success:
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": "Mark failed"}), 500

    except Exception as e:
        logger.error(f"Mark failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/crosshair", methods=["POST"])
def crosshair():
    """Toggle crosshair/positioning laser (0x06 ON, 0x07 OFF)"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        enable = data.get("enable", False)

        success, result = k6_service.crosshair(enable)

        if success:
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": "Crosshair command failed"}), 500

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

    success, message = k6_service.stop()

    if success:
        emit_progress("stopped", 0, message)
        return jsonify({"success": True, "message": message})
    else:
        emit_progress("stopped", 0, message)
        return jsonify({"success": True, "message": message})


@app.route("/api/engrave", methods=["POST"])
def engrave():
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    if "image" not in request.files:
        return jsonify({"success": False, "error": "No image"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    if not image_service.allowed_file(file.filename, ALLOWED_EXTENSIONS):
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
            valid, error_msg = image_service.validate_image_size(
                width, height, K6_MAX_WIDTH, K6_MAX_HEIGHT
            )
            if not valid:
                Path(tmp_path).unlink(missing_ok=True)
                return jsonify({"success": False, "error": error_msg}), 400

        # Get parameters
        depth = int(request.form.get("depth", 100))
        depth = max(1, min(255, depth))
        power = int(request.form.get("power", 1000))
        power = max(0, min(1000, power))

        # Generate CSV log filename
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        csv_path = DATA_DIR / f"burn-{timestamp}.csv"

        # Engrave with CSV logging
        with CSVLogger(str(csv_path)) as csv_logger:
            result = k6_service.engrave(
                tmp_path, power=power, depth=depth, csv_logger=csv_logger
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
    connected = device_manager.is_connected()
    response = {
        "connected": connected,
        "state": "idle" if connected else "disconnected",
        "progress": 0,
        "message": "Ready" if connected else "Not connected",
        "max_width": K6_MAX_WIDTH,
        "max_height": K6_MAX_HEIGHT,
    }
    version = device_manager.version
    if version:
        response["version"] = f"v{version[0]}.{version[1]}.{version[2]}"
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
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        resolution = float(data.get("resolution", 0.05))
        pattern = data.get("pattern", "center")  # center, corners, frame, grid
        size_mm = float(data.get("size_mm", 10.0))

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


        elif pattern == "bottom-test":
            # Bottom-out test: one pixel at top-left, line+marks at bottom
            work_area_px = mm_to_px(80.0)
            mark_spacing_px = mm_to_px(10.0)
            mark_height_px = mm_to_px(10.0)
            height_px = mark_height_px + 10  # bottom region

            img = Image.new("1", (work_area_px, work_area_px), 1)  # full height
            draw = ImageDraw.Draw(img)

            # Top-left pixel
            img.putpixel((0, 0), 0)

            # Bottom line and marks
            bottom_y = work_area_px - 1
            draw.line((0, bottom_y, work_area_px - 1, bottom_y), fill=0, width=2)
            x = 0
            while x <= work_area_px:
                draw.line((x, bottom_y, x, bottom_y - mark_height_px), fill=0, width=2)
                x += mark_spacing_px

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
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        width = int(data.get("width", K6_MAX_WIDTH))
        height = int(data.get("height", K6_MAX_HEIGHT))

        # Validate dimensions
        width = max(1, min(K6_MAX_WIDTH, width))
        height = max(1, min(K6_MAX_HEIGHT, height))

        # Use fast BOUNDS command (0x20)
        success = device_manager.driver.draw_bounds_transport(
            device_manager.transport, width=width, height=height
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
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        from PIL import Image, ImageDraw
        
        data = request.get_json()
        width = int(data.get("width", K6_MAX_WIDTH))
        height = int(data.get("height", K6_MAX_HEIGHT))
        power = int(data.get("power", 500))
        depth = int(data.get("depth", 10))

        # Validate
        width = max(1, min(K6_MAX_WIDTH, width))
        height = max(1, min(K6_MAX_HEIGHT, height))
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
            result = k6_service.engrave(tmp_path, power=power, depth=depth)

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


@app.route("/api/calibration/set-speed-power", methods=["POST"])
def calibration_set_speed_power():
    """Test SET_SPEED_POWER (0x25) command - no observable effect expected"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        speed = int(data.get("speed", 115))
        power = int(data.get("power", 1000))

        # Validate
        speed = max(0, min(65535, speed))  # 16-bit value
        power = max(0, min(1000, power))

        success = device_manager.driver.set_speed_power_transport(
            device_manager.transport, speed=speed, power=power
        )

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": "SET_SPEED_POWER (0x25) command sent",
                    "speed": speed,
                    "power": power,
                }
            )
        else:
            return jsonify({"success": False, "error": "Command failed"}), 500

    except Exception as e:
        logger.error(f"Set speed/power failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/set-focus-angle", methods=["POST"])
def calibration_set_focus_angle():
    """Test SET_FOCUS_ANGLE (0x28) command - no observable effect expected"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        focus = int(data.get("focus", 20))
        angle = int(data.get("angle", 0))

        # Validate
        focus = max(0, min(200, focus))
        angle = max(0, min(255, angle))  # 8-bit value

        success = device_manager.driver.set_focus_angle_transport(
            device_manager.transport, focus=focus, angle=angle
        )

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": "SET_FOCUS_ANGLE (0x28) command sent",
                    "focus": focus,
                    "angle": angle,
                }
            )
        else:
            return jsonify({"success": False, "error": "Command failed"}), 500

    except Exception as e:
        logger.error(f"Set focus/angle failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/generate", methods=["POST"])
def qr_generate():
    """Generate WiFi QR code and return as base64 for preview"""
    try:
        import qrcode
        import io
        import base64

        data = request.get_json()
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        security = data.get("security", "WPA")
        description = data.get("description", "")

        if not ssid:
            return jsonify({"success": False, "error": "SSID required"}), 400

        # WiFi QR format
        wifi_data = f"WIFI:T:{security};S:{ssid};P:{password};H:false;;"

        # Generate QR with high error correction
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2,
        )
        qr.add_data(wifi_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        # K6 burn area: 75mm × 53.98mm = 1500 × 1080 px @ 0.05 mm/px
        burn_width = 1500
        burn_height = 1080

        canvas = Image.new("RGB", (burn_width, burn_height), "white")
        draw = ImageDraw.Draw(canvas)

        # Position QR centered on physical card (card is 85.6mm, burn is 75mm)
        card_width_px = 1712  # 85.6mm at 0.05 mm/px
        offset_right = (card_width_px - burn_width) // 2  # ~106px shift

        # Load fonts
        try:
            font_large = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36
            )
            font_small = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24
            )
        except (OSError, IOError):
            font_large = ImageFont.load_default()
            font_small = ImageFont.load_default()

        # Measure text
        ssid_text = f"SSID: {ssid}"
        ssid_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
        ssid_h = ssid_bbox[3] - ssid_bbox[1]

        desc_h = 0
        line_gap = 8
        if description:
            desc_bbox = draw.textbbox((0, 0), description, font=font_small)
            desc_h = desc_bbox[3] - desc_bbox[1]

        text_block_height = ssid_h + (line_gap + desc_h if desc_h else 0)
        gap = 20
        min_margin = 60  # 3mm at 0.05 mm/px

        # Calculate QR size
        max_qr_w = burn_width - 40 - offset_right
        available_height = burn_height - (2 * min_margin) - gap - text_block_height
        qr_size = min(max_qr_w, available_height)

        qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

        # Position QR
        total_block_height = qr_size + gap + text_block_height
        qr_y = max(min_margin, (burn_height - total_block_height) // 2)
        if qr_y + total_block_height + min_margin > burn_height:
            qr_y = burn_height - total_block_height - min_margin
            if qr_y < min_margin:
                qr_y = min_margin

        qr_x = (burn_width - qr_size) // 2 + offset_right
        canvas.paste(qr_img, (qr_x, qr_y))

        # Add text
        text_y = qr_y + qr_size + gap
        text_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
        text_width = text_bbox[2] - text_bbox[0]
        draw.text(
            ((burn_width - text_width) // 2 + offset_right, text_y),
            ssid_text,
            fill="black",
            font=font_large,
        )

        if description:
            desc_y = text_y + ssid_h + line_gap
            desc_bbox = draw.textbbox((0, 0), description, font=font_small)
            desc_width = desc_bbox[2] - desc_bbox[0]
            draw.text(
                ((burn_width - desc_width) // 2 + offset_right, desc_y),
                description,
                fill="black",
                font=font_small,
            )

        # Convert to base64 for preview
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        img_base64 = base64.b64encode(buffer.getvalue()).decode()

        return jsonify(
            {
                "success": True,
                "image": f"data:image/png;base64,{img_base64}",
                "width": burn_width,
                "height": burn_height,
                "size_mm": "75 × 53.98mm",
            }
        )

    except Exception as e:
        logger.error(f"QR generation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/burn", methods=["POST"])
def qr_burn():
    """Burn WiFi QR code at specified position"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        import qrcode

        # Clear cancel flag
        global burn_cancel_event
        burn_cancel_event.clear()

        data = request.get_json()
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        security = data.get("security", "WPA")
        description = data.get("description", "")
        power = int(data.get("power", 500))
        depth = int(data.get("depth", 10))

        if not ssid:
            return jsonify({"success": False, "error": "SSID required"}), 400

        # Validate
        power = max(0, min(1000, power))
        depth = max(1, min(255, depth))

        # Generate same image as preview
        wifi_data = f"WIFI:T:{security};S:{ssid};P:{password};H:false;;"

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2,
        )
        qr.add_data(wifi_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        burn_width = 1500
        burn_height = 1080

        canvas = Image.new("RGB", (burn_width, burn_height), "white")
        draw = ImageDraw.Draw(canvas)

        card_width_px = 1712
        offset_right = (card_width_px - burn_width) // 2

        try:
            font_large = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36
            )
            font_small = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24
            )
        except (OSError, IOError):
            font_large = ImageFont.load_default()
            font_small = ImageFont.load_default()

        ssid_text = f"SSID: {ssid}"
        ssid_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
        ssid_h = ssid_bbox[3] - ssid_bbox[1]

        desc_h = 0
        line_gap = 8
        if description:
            desc_bbox = draw.textbbox((0, 0), description, font=font_small)
            desc_h = desc_bbox[3] - desc_bbox[1]

        text_block_height = ssid_h + (line_gap + desc_h if desc_h else 0)
        gap = 20
        min_margin = 60

        max_qr_w = burn_width - 40 - offset_right
        available_height = burn_height - (2 * min_margin) - gap - text_block_height
        qr_size = min(max_qr_w, available_height)

        qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

        total_block_height = qr_size + gap + text_block_height
        qr_y = max(min_margin, (burn_height - total_block_height) // 2)
        if qr_y + total_block_height + min_margin > burn_height:
            qr_y = burn_height - total_block_height - min_margin
            if qr_y < min_margin:
                qr_y = min_margin

        qr_x = (burn_width - qr_size) // 2 + offset_right
        canvas.paste(qr_img, (qr_x, qr_y))

        text_y = qr_y + qr_size + gap
        text_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
        text_width = text_bbox[2] - text_bbox[0]
        draw.text(
            ((burn_width - text_width) // 2 + offset_right, text_y),
            ssid_text,
            fill="black",
            font=font_large,
        )

        if description:
            desc_y = text_y + ssid_h + line_gap
            desc_bbox = draw.textbbox((0, 0), description, font=font_small)
            desc_width = desc_bbox[2] - desc_bbox[0]
            draw.text(
                ((burn_width - desc_width) // 2 + offset_right, desc_y),
                description,
                fill="black",
                font=font_small,
            )

        # Save and burn
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            canvas.save(tmp.name)
            tmp_path = tmp.name

        # Clear progress queue
        while not progress_queue.empty():
            try:
                progress_queue.get_nowait()
            except queue.Empty:
                break

        emit_progress("start", 0, "Starting WiFi QR burn")
        emit_progress("prepare", 10, f"SSID: {ssid}, {burn_width}x{burn_height}px")

        try:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            csv_path = DATA_DIR / f"qr-wifi-{timestamp}.csv"

            # Position at center (default for left-aligned card)
            center_x = (burn_width // 2) + K6_CENTER_X_OFFSET
            center_y = K6_DEFAULT_CENTER_Y

            with ProgressCSVLogger(str(csv_path)) as csv_logger:
                emit_progress("setup", 0, "Starting burn sequence...")
                result = k6_service.engrave(
                    tmp_path,
                    power=power,
                    depth=depth,
                    center_x=center_x,
                    center_y=center_y,
                    csv_logger=csv_logger,
                )

            Path(tmp_path).unlink(missing_ok=True)

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
                    }
                )
            else:
                emit_progress("error", 0, result["message"])
                return jsonify({"success": False, "error": result["message"]}), 500

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except KeyboardInterrupt:
        logger.info("QR burn cancelled by user")
        emit_progress("stopped", 0, "Burn cancelled")
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Cancelled by user"}), 400
    except Exception as e:
        logger.error(f"QR burn failed: {e}")
        emit_progress("error", 0, str(e))
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/alignment", methods=["POST"])
def qr_alignment():
    """Burn alignment box for credit card positioning"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        data = request.get_json()
        power = int(data.get("power", 300))
        depth = int(data.get("depth", 5))

        power = max(0, min(1000, power))
        depth = max(1, min(255, depth))

        # Generate alignment box (75mm × 53.98mm)
        burn_width = 1500
        burn_height = 1080

        canvas = Image.new("RGB", (burn_width, burn_height), "white")
        draw = ImageDraw.Draw(canvas)

        # Card positioning offset
        card_width_px = 1712
        offset_right = (card_width_px - burn_width) // 2

        # Draw box with border
        border = 10
        left = border + offset_right
        right = burn_width - border
        draw.rectangle(
            [(left, border), (right, burn_height - border)], outline="black", width=3
        )

        # Corner marks
        corner_size = 20
        for x, y in [
            (left, border),
            (right, border),
            (left, burn_height - border),
            (right, burn_height - border),
        ]:
            draw.line([(x - corner_size, y), (x + corner_size, y)], fill="black", width=2)
            draw.line([(x, y - corner_size), (x, y + corner_size)], fill="black", width=2)

        # Label
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24
            )
        except (OSError, IOError):
            font = ImageFont.load_default()

        label = "K6 Burn Area (75 × 53.98 mm)"
        bbox = draw.textbbox((0, 0), label, font=font)
        text_width = bbox[2] - bbox[0]
        draw.text(
            ((burn_width - text_width) // 2, burn_height // 2 - 12),
            label,
            fill="black",
            font=font,
        )

        # Save and burn
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            canvas.save(tmp.name)
            tmp_path = tmp.name

        try:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            csv_path = DATA_DIR / f"qr-alignment-{timestamp}.csv"

            # Position at center
            center_x = (burn_width // 2) + K6_CENTER_X_OFFSET
            center_y = K6_DEFAULT_CENTER_Y

            with CSVLogger(str(csv_path)) as csv_logger:
                result = k6_service.engrave(
                    tmp_path,
                    power=power,
                    depth=depth,
                    center_x=center_x,
                    center_y=center_y,
                    csv_logger=csv_logger,
                )

            if result["ok"]:
                return jsonify(
                    {
                        "success": True,
                        "message": "Alignment box burned",
                        "total_time": result["total_time"],
                        "csv_log": str(csv_path),
                    }
                )
            else:
                return jsonify({"success": False, "error": result["message"]}), 500

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        logger.error(f"Alignment burn failed: {e}")
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/burn", methods=["POST"])
def calibration_burn():  # noqa: C901
    """Burn calibration pattern"""
    if not device_manager.is_connected():
        return jsonify({"success": False, "error": "Not connected"}), 400

    try:
        # Clear cancel flag at start of new burn
        global burn_cancel_event
        burn_cancel_event.clear()

        # Re-home the device before starting new burn (in case STOP was pressed)
        try:
            k6_service.home()
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

        elif pattern == "bottom-test":
            # Bottom-out test pattern: horizontal line at bottom with 1cm vertical marks
            # Tests Y-axis limits to find where bottom row gets cropped
            work_area_px = mm_to_px(80.0)
            mark_spacing_px = mm_to_px(10.0)  # 1cm = 10mm spacing
            mark_height_px = mm_to_px(10.0)   # 1cm = 10mm height

            # Only create image for the region with content (bottom 10mm + margin)
            # This avoids sending 1400+ blank lines that the device rejects
            height_px = mark_height_px + 10  # marks + small margin
            
            img = Image.new("1", (work_area_px, height_px), 1)
            draw = ImageDraw.Draw(img)

            # Draw horizontal line at the bottom of this region
            bottom_y = height_px - 1
            draw.line((0, bottom_y, work_area_px - 1, bottom_y), fill=0, width=2)

            # Draw vertical marks every 1cm, rising 1cm from the line
            x = 0
            while x <= work_area_px:
                # Vertical mark from bottom_y up to (bottom_y - mark_height_px)
                draw.line((x, bottom_y, x, bottom_y - mark_height_px), fill=0, width=2)
                x += mark_spacing_px

        else:
            return jsonify({"success": False, "error": "Invalid pattern"}), 400

        # Calculate center coordinates for positioning
        # Work area: 1600x1520px (76mm actual Y-axis, not 80mm)
        center_x = (img.width // 2) + K6_CENTER_X_OFFSET
        
        if pattern == "bottom-test":
            # Position at bottom of work area, leave 10px margin from firmware limit
            center_y = (K6_MAX_HEIGHT - 10) - (img.height // 2)
        else:
            # Default: center in work area
            center_y = K6_CENTER_Y

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

            with ProgressCSVLogger(str(csv_path)) as csv_logger:
                emit_progress(
                    "setup", 0, "Image processing complete, starting protocol..."
                )
                result = k6_service.engrave(
                    tmp_path,
                    power=power,
                    depth=depth,
                    center_x=center_x,
                    center_y=center_y,
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
