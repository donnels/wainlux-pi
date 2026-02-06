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
import os
from datetime import datetime
import json
import time
import queue
import threading
from PIL import Image, ImageDraw

# Add k6 library to path (located in /app/k6 in container)
# From /app/app/main.py, parent is /app/app, parent.parent is /app
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # Add /app/app for services module

from k6.csv_logger import CSVLogger
from k6.commands import K6CommandBuilder, CommandSequence
from services import ImageService, K6Service, QRService, PatternService, PipelineService, PreviewService
from services.k6_service import K6DeviceManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.secret_key = 'k6-laser-session-key-change-in-production'

# Device manager and services
device_manager = K6DeviceManager()
k6_service = K6Service(device_manager)
image_service = ImageService()
qr_service = QRService()

# Operational mode (workflow testing): silent (default) / verbose / single-step
# Controls logging and intermediate file generation for debugging/learning
# Stored in session, persists per-browser
VALID_MODES = {'silent', 'verbose', 'single-step'}
DEFAULT_MODE = 'silent'

# Dry run mode: test with real K6 but disable laser firing
# Works with both real hardware and mock mode
DEFAULT_DRY_RUN = False

# Log startup configuration
logger.info(f"=== K6 Startup Configuration ===")
logger.info(f"K6_MOCK_DEVICE env: {os.getenv('K6_MOCK_DEVICE', 'not set')}")
logger.info(f"Mock mode active: {device_manager.mock_mode}")
logger.info(f"Dry run default: {DEFAULT_DRY_RUN}")
logger.info(f"Operation mode default: {DEFAULT_MODE}")
logger.info(f"================================")

def get_operation_mode():
    """Get current operation mode from session."""
    from flask import session
    return session.get('operation_mode', DEFAULT_MODE)

def set_operation_mode(mode: str):
    """Set operation mode in session."""
    from flask import session
    if mode in VALID_MODES:
        session['operation_mode'] = mode
        return True
    return False

def get_dry_run():
    """Get dry run setting from session."""
    from flask import session
    return session.get('dry_run', DEFAULT_DRY_RUN)

def set_dry_run(enabled: bool):
    """Set dry run setting in session."""
    from flask import session
    session['dry_run'] = bool(enabled)
    return True

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
    return render_template("index.html")


@app.route("/settings")
def settings():
    """Settings page for operation mode selection"""
    return render_template("settings.html")


@app.route("/calibration")
def calibration():
    """Advanced calibration and testing page"""
    return render_template("calibration.html")


@app.route("/api/materials/list", methods=["GET"])
def list_materials():
    """List available material definitions"""
    materials_dir = Path(__file__).parents[1] / "settings" / "material"
    try:
        materials = []
        if materials_dir.exists():
            for json_file in materials_dir.glob("*.json"):
                with open(json_file, 'r') as f:
                    import json as json_module
                    material_data = json_module.load(f)
                    material_data['id'] = json_file.stem  # filename without .json
                    materials.append(material_data)
        materials.sort(key=lambda x: x.get('name', ''))
        return jsonify({"success": True, "materials": materials})
    except Exception as e:
        logger.error(f"List materials failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/shapes/list", methods=["GET"])
def list_shapes():
    """List available object shape definitions"""
    shapes_dir = Path(__file__).parents[1] / "settings" / "shapes"
    try:
        shapes = []
        if shapes_dir.exists():
            for json_file in shapes_dir.glob("*.json"):
                with open(json_file, 'r') as f:
                    import json as json_module
                    shape_data = json_module.load(f)
                    shape_data['id'] = json_file.stem
                    shapes.append(shape_data)
        shapes.sort(key=lambda x: x.get('name', ''))
        return jsonify({"success": True, "shapes": shapes})
    except Exception as e:
        logger.error(f"List shapes failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/qr")
def qr_page():
    """WiFi QR code generation and burn page"""
    return render_template("qr.html")


@app.route("/burn")
def burn_page():
    """Dedicated burn page with detailed progress tracking"""
    return render_template("burn.html")


@app.route("/alignment-builder")
def alignment_builder():
    """Custom alignment pattern builder"""
    return render_template("alignment-builder.html")


@app.route("/preview-tests")
def preview_tests():
    """Preview engine test page"""
    return render_template("preview-tests.html")


@app.route("/api/images/list", methods=["GET"])
def list_images():
    """List available sample images from /app/images directory"""
    images_dir = Path(__file__).parents[1] / "images"
    if not images_dir.exists():
        return jsonify({"success": True, "images": []})    
    try:
        image_files = []
        for ext in ["*.png", "*.jpg", "*.jpeg", "*.bmp", "*.gif"]:
            image_files.extend(images_dir.glob(ext))
        
        # Return relative paths for URL fetching
        images = [{"name": f.name, "url": f"/api/images/serve/{f.name}"} for f in sorted(image_files)]
        return jsonify({"success": True, "images": images})
    except Exception as e:
        logger.error(f"List images failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/images/serve/<filename>", methods=["GET"])
def serve_image(filename):
    """Serve sample image file"""
    images_dir = Path(__file__).parents[1] / "images"
    file_path = images_dir / filename
    
    # Security: prevent directory traversal
    if not file_path.resolve().parent == images_dir.resolve():
        return jsonify({"success": False, "error": "Invalid filename"}), 400
    
    if not file_path.exists():
        return jsonify({"success": False, "error": "Image not found"}), 404
    
    from flask import send_file
    return send_file(str(file_path), mimetype='image/png')


@app.route("/api/connect", methods=["POST"])
def connect():
    try:
        success, version_str = device_manager.connect(
            port="/dev/ttyUSB0", baudrate=115200, timeout=2.0
        )
        if success:
            logger.info(f"Connected to K6 {version_str}")
            return jsonify({"success": True, "connected": True, "version": version_str})
        else:
            return jsonify({"success": False, "connected": False, "error": "Connection failed"}), 500
    except Exception as e:
        logger.error(f"Connect failed: {e}")
        return jsonify({"success": False, "connected": False, "error": str(e)}), 500


@app.route("/api/test/bounds", methods=["POST"])
def test_bounds():
    try:
        success = k6_service.draw_bounds()
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Bounds test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/test/home", methods=["POST"])
def test_home():
    try:
        success = k6_service.home()
        return jsonify({"success": success})
    except Exception as e:
        logger.error(f"Home test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/jog", methods=["POST"])
def jog():
    """Move laser head to absolute position using BOUNDS positioning"""
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


@app.route("/api/engrave/prepare", methods=["POST"])
def engrave_prepare():
    """Prepare image for engraving - unified workflow step 1
    
    Saves uploaded image and returns preview. All images go through this path.
    Burn endpoint then uses the temp file.
    """
    if "image" not in request.files:
        return jsonify({"success": False, "error": "No image"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    # NO extension check - PIL validates actual image data below
    # original_filename is metadata only (debugging/logging)

    try:
        # Save with timestamp hash - filename is irrelevant, PIL validates format
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        temp_path = DATA_DIR / f"upload_{timestamp}.png"
        original_filename = file.filename  # Metadata only (for logging)
        
        # Detect SVG by content (XML declaration or <svg tag)
        file_content = file.read()
        file.seek(0)  # Reset for save
        
        is_svg = False
        if file_content[:100].lower().find(b'<svg') != -1 or file_content[:100].lower().find(b'<?xml') != -1:
            is_svg = True
            logger.info(f"Detected SVG: {original_filename}")
        
        if is_svg:
            # Save SVG to temp file
            svg_temp = DATA_DIR / f"upload_{timestamp}.svg"
            with open(svg_temp, 'wb') as f:
                f.write(file_content)
            
            try:
                # Convert SVG → PNG (800px width, auto height)
                img = image_service.svg_to_png(str(svg_temp), width=800)
                # Save rasterized PNG
                img.save(str(temp_path))
            finally:
                svg_temp.unlink(missing_ok=True)
        else:
            # Standard raster image upload
            file.save(str(temp_path))

        # PIL validates image format - will fail here if invalid
        with Image.open(temp_path) as img:
            width, height = img.size
            
            # CRITICAL: Force all images to 1-bit to prevent antialiasing artifacts
            # QR codes are already 1-bit, but canvas composites may introduce gray pixels
            # Threshold: < 128 = black (0), >= 128 = white (255)
            if img.mode != "1":
                gray = img.convert("L")
                img = gray.point(lambda x: 0 if x < 128 else 255, mode="1")
                # Save back to temp file as clean 1-bit
                img.save(str(temp_path))
            
            # Generate base64 (OPTION 2: embedded in job structure)
            data_url = image_service.image_to_base64(img)

        return jsonify({
            "success": True,
            "image": data_url,  # For immediate preview display
            "image_base64": data_url,  # NEW: For embedding in job structure
            "temp_path": str(temp_path),
            "original_filename": original_filename,  # Track original name
            "source": "upload",  # Track source type
            "width": width,
            "height": height,
        })

    except Exception as e:
        logger.error(f"Engrave prepare failed: {e}")
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/engrave", methods=["POST"])
def engrave():
    """Burn image - mode-aware workflow
    
    Respects operation_mode and dry_run settings.
    Silent: monolithic, minimal logs
    Verbose: saves all intermediate files
    Single-step: NOT IMPLEMENTED (use pipeline endpoints directly)
    """
    try:
        # Ensure device is connected - always reconnect if transport is None
        if not device_manager.is_connected() or device_manager.transport is None:
            logger.info("Device not connected, attempting auto-connect...")
            success, version = device_manager.connect(port="/dev/ttyUSB0", baudrate=115200, timeout=2.0)
            if not success:
                return jsonify({"success": False, "error": "Device not connected. Please verify connection first."}), 400
            logger.info(f"Auto-connected to K6 {version}")
        
        mode = get_operation_mode()
        dry_run = get_dry_run()
        
        # Accept both JSON (with temp_path) and FormData (direct upload)
        data = request.get_json(silent=True) or {}
        temp_path = data.get("temp_path", "")
        
        # Fallback: if no temp_path, accept direct file upload (backward compat)
        if not temp_path and "image" in request.files:
            file = request.files["image"]
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                file.save(tmp.name)
                temp_path = tmp.name
        
        if not temp_path or not Path(temp_path).exists():
            return jsonify({"success": False, "error": "No image file"}), 400

        # Get parameters
        depth = int(data.get("depth", 100))
        depth = max(1, min(255, depth))
        power = int(data.get("power", 1000))
        power = max(0, min(1000, power))
        
        # Positioning: Use provided coordinates or let driver auto-calculate
        # Driver auto-calc: center_x = (width // 2) + 67, center_y = height // 2
        center_x = data.get("center_x", None)
        center_y = data.get("center_y", None)
        
        if center_x is not None:
            center_x = int(center_x)
        if center_y is not None:
            center_y = int(center_y)

        # Silent: monolithic, with progress updates (default)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        csv_path = DATA_DIR / f"burn-{timestamp}.csv"
        
        # Clear progress queue before starting
        while not progress_queue.empty():
            try:
                progress_queue.get_nowait()
            except queue.Empty:
                break
        
        # Clear cancel flag
        global burn_cancel_event
        burn_cancel_event.clear()
        
        logger.info(f"=== ENGRAVE START === temp_path={temp_path}, power={power}, depth={depth}")
        logger.info(f"Device connected: {device_manager.is_connected()}")
        
        emit_progress("start", 0, "Starting image burn")
        
        with ProgressCSVLogger(str(csv_path)) as csv_logger:
            logger.info("ProgressCSVLogger context entered")
            emit_progress("setup", 0, "Initializing burn sequence...")
            result = k6_service.engrave(
                temp_path, power=power, depth=depth, 
                center_x=center_x, center_y=center_y,
                csv_logger=csv_logger
            )
            logger.info(f"k6_service.engrave returned: {result}")
        
        # Clean up temp file
        Path(temp_path).unlink(missing_ok=True)
        
        if burn_cancel_event.is_set():
            emit_progress("stopped", 0, "Burn cancelled by user")
            return jsonify({"success": False, "error": "Cancelled by user"}), 400
        
        if result["ok"]:
            emit_progress("complete", 100, f"Burn complete in {result['total_time']:.1f}s")
            return jsonify({
                "success": True,
                "message": result["message"],
                "total_time": result["total_time"],
                "chunks": result["chunks"],
                "csv_log": str(csv_path),
                "dry_run": device_manager.dry_run
            })
        else:
            emit_progress("error", 0, result["message"])
            return jsonify({"success": False, "error": result["message"]}), 500

    except KeyboardInterrupt:
        logger.info("Burn cancelled by user")
        emit_progress("stopped", 0, "Burn cancelled")
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Cancelled by user"}), 400
    except Exception as e:
        logger.error(f"Engrave failed: {e}")
        emit_progress("error", 0, str(e))
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/pipeline/process", methods=["POST"])
def pipeline_process():
    """Process image to burn-ready format - pipeline step 2
    
    Unix pipe model: upload.png | process → processed.npy + preview.png
    Single-step mode: stores data, awaits approval before next step
    """
    try:
        data = request.get_json()
        temp_path = data.get("temp_path", "")
        threshold = int(data.get("threshold", 128))
        invert = bool(data.get("invert", False))
        
        if not temp_path or not Path(temp_path).exists():
            return jsonify({"success": False, "error": "Invalid temp_path"}), 400

        # Process image
        result = PipelineService.process_image(temp_path, DATA_DIR, threshold, invert)

        # Load preview for base64
        with Image.open(result["preview_path"]) as preview_img:
            preview_b64 = image_service.image_to_base64(preview_img)

        return jsonify({
            "success": True,
            "processed_path": result["processed_path"],
            "preview": preview_b64,
            "width": result["width"],
            "height": result["height"],
            "stats": result["stats"]
        })

    except Exception as e:
        logger.error(f"Pipeline process failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/pipeline/build", methods=["POST"])
def pipeline_build():
    """Build command sequence from processed data - pipeline step 3
    
    Unix pipe model: processed.npy | build → commands.bin + metadata.json
    Exposes all settings (power, depth, position) and command statistics
    """
    try:
        data = request.get_json()
        processed_path = data.get("processed_path", "")
        power = int(data.get("power", 1000))
        depth = int(data.get("depth", 100))
        center_x = int(data.get("center_x", K6_DEFAULT_CENTER_X))
        center_y = int(data.get("center_y", K6_DEFAULT_CENTER_Y))
        
        if not processed_path or not Path(processed_path).exists():
            return jsonify({"success": False, "error": "Invalid processed_path"}), 400

        # Build commands
        result = PipelineService.build_commands(
            processed_path, power, depth, center_x, center_y, DATA_DIR
        )

        return jsonify({
            "success": True,
            "command_path": result["command_path"],
            "metadata_path": result["metadata_path"],
            "sequence": result["sequence"],
            "bounds": result["bounds"],
            "validation": result["validation"]
        })

    except ValueError as e:
        logger.error(f"Pipeline build validation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Pipeline build failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/pipeline/execute", methods=["POST"])
def pipeline_execute():
    """Execute pre-built command sequence - pipeline step 4
    
    Unix pipe model: commands.bin | execute | tee logs
    Raw byte logging (like `tee`) - captures ALL I/O for debugging
    """
    try:
        data = request.get_json()
        command_path = data.get("command_path", "")
        log_mode = data.get("log_mode", "both")  # "csv" | "bytes" | "both"
        
        if not command_path or not Path(command_path).exists():
            return jsonify({"success": False, "error": "Invalid command_path"}), 400

        # Setup logging
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        csv_logger = None
        byte_logger = None
        
        if log_mode in ("csv", "both"):
            from k6.csv_logger import CSVLogger
            csv_path = DATA_DIR / f"execute_{timestamp}.csv"
            csv_logger = CSVLogger(str(csv_path))
        
        if log_mode in ("bytes", "both"):
            from k6.byte_logger import ByteDumpLogger
            byte_path = DATA_DIR / f"execute_{timestamp}"
            byte_logger = ByteDumpLogger(str(byte_path))

        try:
            # Execute commands with logging
            result = device_manager.driver.execute_from_file(
                device_manager.transport,
                command_path,
                csv_logger=csv_logger,
                byte_logger=byte_logger
            )

            response = {
                "success": result["ok"],
                "commands_sent": result["commands_sent"],
                "device_responses": result["device_responses"]
            }

            if csv_logger:
                response["csv_log"] = str(csv_path)
            if byte_logger:
                response["byte_dump"] = str(byte_path) + ".dump"
                response["byte_dump_text"] = str(byte_path) + ".dump.txt"

            return jsonify(response)

        finally:
            if csv_logger:
                csv_logger.close()
            if byte_logger:
                byte_logger.close()

    except Exception as e:
        logger.error(f"Pipeline execute failed: {e}")
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
        # device_manager.version may already be a string (mock) or a tuple
        if isinstance(version, (tuple, list)) and len(version) >= 3:
            response["version"] = f"v{version[0]}.{version[1]}.{version[2]}"
        else:
            response["version"] = str(version)
    
    # Add operation mode info
    response["operation_mode"] = get_operation_mode()
    response["dry_run"] = device_manager.dry_run  # Use driver state as source of truth
    response["mock_mode"] = device_manager.mock_mode
    
    return jsonify(response)


@app.route("/api/debug/env", methods=["GET"])
def debug_env():
    """Debug endpoint to check environment variables and configuration"""
    import os
    return jsonify({
        "env": {
            "K6_MOCK_DEVICE": os.getenv("K6_MOCK_DEVICE", "not set"),
            "FLASK_ENV": os.getenv("FLASK_ENV", "not set"),
            "PYTHONUNBUFFERED": os.getenv("PYTHONUNBUFFERED", "not set"),
        },
        "device_manager": {
            "mock_mode": device_manager.mock_mode,
            "dry_run": device_manager.dry_run,
            "connected": device_manager.is_connected(),
            "version": str(device_manager.version) if device_manager.version else None,
            "transport_type": type(device_manager.transport).__name__ if device_manager.transport else "None",
        },
        "defaults": {
            "operation_mode": DEFAULT_MODE,
            "dry_run": DEFAULT_DRY_RUN,
        }
    })


@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Get current settings including operation mode and dry run"""
    return jsonify({
        "success": True,
        "operation_mode": get_operation_mode(),
        "dry_run": get_dry_run(),
        "valid_modes": list(VALID_MODES)
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Update settings including operation mode and dry run"""
    try:
        data = request.get_json()
        
        # Update operation mode if provided
        if "operation_mode" in data:
            mode = data["operation_mode"]
            if not set_operation_mode(mode):
                return jsonify({"success": False, "error": f"Invalid mode: {mode}"}), 400
        
        # Update dry run if provided (driver-level setting)
        if "dry_run" in data:
            dry_run_enabled = bool(data["dry_run"])
            set_dry_run(dry_run_enabled)
            device_manager.dry_run = dry_run_enabled  # Set on device manager for driver access
        
        return jsonify({
            "success": True,
            "operation_mode": get_operation_mode(),
            "dry_run": get_dry_run()
        })
    
    except Exception as e:
        logger.error(f"Settings update failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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
def calibration_generate():
    """Generate calibration test pattern - unified workflow step 1"""
    try:
        data = request.get_json()
        resolution = float(data.get("resolution", 0.05))
        pattern = data.get("pattern", "center")
        size_mm = float(data.get("size_mm", 10.0))

        # Generate pattern using service (DRY - single source)
        img = PatternService.generate(pattern, resolution, size_mm)

        # Save to temp file
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        temp_path = DATA_DIR / f"calibration_preview_{timestamp}.png"
        img.save(str(temp_path))

        # Generate preview (unified preview path)
        data_url = image_service.image_to_base64(img)

        return jsonify(
            {
                "success": True,
                "image": data_url,
                "image_base64": data_url,  # OPTION 2: embed base64 in job
                "temp_path": str(temp_path),
                "width": img.width,
                "height": img.height,
                "pattern": pattern,
                "resolution": resolution,
                "size_mm": size_mm,
            }
        )

    except Exception as e:
        logger.error(f"Calibration generate failed: {e}")
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/preview", methods=["POST"])
def calibration_preview():
    """Draw preview bounds using fast 0x20 BOUNDS command"""
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
    try:
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
    """Generate WiFi QR code ONCE - save to temp file and return preview"""
    try:
        data = request.get_json()
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        security = data.get("security", "WPA")
        description = data.get("description", "")
        show_password = data.get("show_password", False)

        if not ssid:
            return jsonify({"success": False, "error": "SSID required"}), 400

        # Generate QR image ONCE using QRService
        canvas, metadata = qr_service.generate_wifi_qr(ssid, password, security, description, show_password)
        
        # Save to temp file (will be used by burn endpoint)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        temp_path = DATA_DIR / f"qr_preview_{timestamp}.png"
        canvas.save(str(temp_path))
        
        # Convert to base64 for preview display (generic image preview)
        data_url = image_service.image_to_base64(canvas)

        return jsonify(
            {
                "success": True,
                "image": data_url,
                "image_base64": data_url,  # OPTION 2: embed base64 in job
                "temp_path": str(temp_path),  # Client will pass this to burn endpoint
                "width": metadata["width"],
                "height": metadata["height"],
                "size_mm": "75 × 53.98mm",
            }
        )

    except Exception as e:
        logger.error(f"QR generation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/preview", methods=["POST"])
def qr_preview():
    """Generate QR command sequence preview without burning
    
    Tests the command abstraction layer with a real QR pattern.
    Returns command sequence with stats, bounds, and estimated time.
    """
    try:
        import numpy as np

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

        # Use QRService to generate QR image (DRY - single source of truth)
        canvas, metadata = qr_service.generate_wifi_qr(ssid, password, security, description)
        burn_width = metadata["width"]
        burn_height = metadata["height"]

        # Convert to grayscale and threshold to 1-bit
        gray = canvas.convert("L")
        threshold = 128
        binary = gray.point(lambda x: 0 if x < threshold else 255, mode="1")
        img_array = np.array(binary, dtype=np.uint8)

        # Pad to 8-pixel boundary
        height, width = img_array.shape
        padded_width = ((width + 7) // 8) * 8
        if padded_width > width:
            padding = np.ones((height, padded_width - width), dtype=np.uint8) * 255
            img_array = np.hstack([img_array, padding])

        # Bit-pack: 8 pixels per byte (MSB first)
        img_array = img_array.reshape(height, -1, 8)
        img_array = np.packbits(img_array, axis=2, bitorder="big").squeeze(axis=2)

        # === Generate Command Sequence (NO EXECUTION) ===
        seq = CommandSequence(description=f"WiFi QR: {ssid}")

        # Setup phase
        seq.add(K6CommandBuilder.build_framing())
        
        center_x = (burn_width // 2) + K6_CENTER_X_OFFSET
        center_y = K6_DEFAULT_CENTER_Y
        
        seq.add(K6CommandBuilder.build_job_header(
            width=burn_width,
            height=burn_height,
            depth=depth,
            power=power,
            center_x=center_x,
            center_y=center_y
        ))
        
        seq.add(K6CommandBuilder.build_connect(1))
        seq.add(K6CommandBuilder.build_connect(2))

        # Data chunks
        for line_num in range(height):
            line_data = img_array[line_num].tobytes()
            seq.add(K6CommandBuilder.build_data_packet(line_data, line_num))

        # Finalization
        seq.add(K6CommandBuilder.build_init(1))
        seq.add(K6CommandBuilder.build_init(2))
        seq.add(K6CommandBuilder.build_connect(1))
        seq.add(K6CommandBuilder.build_connect(2))

        # Get sequence info
        bounds = seq.get_bounds()
        stats = seq.stats
        
        # Validate bounds
        warnings = []
        if bounds['max_y'] > K6_MAX_HEIGHT:
            warnings.append(f"WARNING: max_y {bounds['max_y']} exceeds limit {K6_MAX_HEIGHT}")
        if bounds['max_x'] > K6_MAX_WIDTH:
            warnings.append(f"WARNING: max_x {bounds['max_x']} exceeds limit {K6_MAX_WIDTH}")

        return jsonify({
            "success": True,
            "ssid": ssid,
            "description": description,
            "dimensions": {
                "width": burn_width,
                "height": burn_height,
                "size_mm": "75 × 53.98mm"
            },
            "parameters": {
                "power": power,
                "depth": depth
            },
            "sequence": {
                "total_commands": stats['total_commands'],
                "data_chunks": stats['data_chunks'],
                "total_bytes": stats['total_bytes'],
                "estimated_time_sec": stats['estimated_time_sec'],
                "estimated_time_min": round(stats['estimated_time_sec'] / 60, 1)
            },
            "bounds": bounds,
            "warnings": warnings,
            "summary": seq.summary()
        })

    except Exception as e:
        logger.error(f"QR preview failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/burn", methods=["POST"])
def qr_burn():
    """Burn WiFi QR code using previously generated image"""
    try:
        # Clear cancel flag
        global burn_cancel_event
        burn_cancel_event.clear()

        data = request.get_json()
        temp_path = data.get("temp_path", "")
        ssid = data.get("ssid", "")  # For logging/metadata
        power = int(data.get("power", 500))
        depth = int(data.get("depth", 10))

        # Check if we have a temp file from preview, otherwise generate on-demand
        if not temp_path or not Path(temp_path).exists():
            logger.warning("No preview image found, generating QR on-demand")
            # Fallback: generate if preview wasn't called (shouldn't happen in normal flow)
            password = data.get("password", "")
            security = data.get("security", "WPA")
            description = data.get("description", "")
            
            if not ssid:
                return jsonify({"success": False, "error": "SSID required"}), 400
            
            canvas, _ = qr_service.generate_wifi_qr(ssid, password, security, description)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            temp_path = str(DATA_DIR / f"qr_ondemand_{timestamp}.png")
            canvas.save(temp_path)

        # Validate
        power = max(0, min(1000, power))
        depth = max(1, min(255, depth))

        # Use the temp_path from preview (image already generated)
        logger.info(f"Using pre-generated QR image: {temp_path}")

        # Clear progress queue
        while not progress_queue.empty():
            try:
                progress_queue.get_nowait()
            except queue.Empty:
                break

        # Get dimensions from the image
        with Image.open(temp_path) as img:
            burn_width, burn_height = img.size

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
                    temp_path,
                    power=power,
                    depth=depth,
                    center_x=center_x,
                    center_y=center_y,
                    csv_logger=csv_logger,
                )

            # Clean up temp file after successful burn
            Path(temp_path).unlink(missing_ok=True)

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
            # Clean up temp file
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    except KeyboardInterrupt:
        logger.info("QR burn cancelled by user")
        emit_progress("stopped", 0, "Burn cancelled")
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Cancelled by user"}), 400
    except Exception as e:
        logger.error(f"QR burn failed: {e}")
        emit_progress("error", 0, str(e))
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/alignment", methods=["POST"])
def qr_alignment():
    """Burn alignment box for credit card positioning"""
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


@app.route("/api/alignment/builder", methods=["POST"])
def alignment_builder_api():
    """Generate custom alignment pattern with edge selection
    
    Handles objects larger than burn area by allowing user to specify
    which edges to align to. Returns alignment pattern + metadata for
    subsequent image positioning.
    """
    try:
        data = request.get_json()
        width_mm = float(data.get("width_mm", 50.0))
        height_mm = float(data.get("height_mm", 30.0))
        align_x = data.get("align_x", "center")  # left/right/center
        align_y = data.get("align_y", "center")  # top/bottom/center
        burn_offset_x = float(data.get("burn_offset_x", 0.0))
        burn_offset_y = float(data.get("burn_offset_y", 0.0))
        resolution = float(data.get("resolution", 0.05))
        
        # Validate dimensions
        if width_mm <= 0 or height_mm <= 0:
            return jsonify({"success": False, "error": "Invalid dimensions"}), 400
        
        # Determine which method to use based on size and alignment
        exceeds_x = width_mm > K6_MAX_WIDTH * resolution
        exceeds_y = height_mm > K6_MAX_HEIGHT * resolution
        
        # For credit card specific case (85.6mm × 53.98mm)
        if (abs(width_mm - 85.6) < 1.0 and abs(height_mm - 53.98) < 1.0 and 
            align_x == "left"):
            img, metadata = PatternService.generate_card_alignment(
                burn_width_mm=75.0,
                burn_height_mm=53.98,
                card_width_mm=85.6,
                resolution=resolution
            )
            
            # Add alignment info to metadata
            metadata["align_x"] = "left"
            metadata["align_y"] = "center"
            metadata["object_width_mm"] = width_mm
            metadata["object_height_mm"] = height_mm
            metadata["burn_offset_x_mm"] = 0
            metadata["burn_offset_y_mm"] = 0
            
        elif not exceeds_x and not exceeds_y:
            # Standard alignment - object fits in burn area
            img, metadata = PatternService.generate_alignment(
                width_mm, height_mm, resolution
            )
            
            # Add alignment info
            metadata["align_x"] = "center"
            metadata["align_y"] = "center"
            metadata["burn_offset_x_mm"] = burn_offset_x
            metadata["burn_offset_y_mm"] = burn_offset_y
            
        else:
            # Oversized object - need edge alignment logic
            # This is a simplified version - full implementation would need
            # PatternService.generate_aligned_box() method
            return jsonify({
                "success": False,
                "error": f"Oversized object support not yet implemented for {width_mm}×{height_mm}mm. Use standard alignment or credit card preset."
            }), 400
        
        # Convert image to base64 for preview
        preview_b64 = image_service.image_to_base64(img)
        
        # Save temp file for burning
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        temp_path = DATA_DIR / f"alignment_builder_{timestamp}.png"
        img.save(str(temp_path))
        
        return jsonify({
            "success": True,
            "image": preview_b64,
            "temp_path": str(temp_path),
            "metadata": metadata
        })
        
    except ValueError as e:
        logger.error(f"Alignment builder validation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Alignment builder failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/job/burn", methods=["POST"])
def job_burn():
    """Burn from complete job JSON structure
    
    Accepts full .k6job JSON with embedded base64 images.
    Processes all enabled burn_jobs in sequence.
    """
    try:
        # Ensure device is connected - always reconnect if transport is None
        if not device_manager.is_connected() or device_manager.transport is None:
            logger.info("Device not connected, attempting auto-connect...")
            success, version = device_manager.connect(port="/dev/ttyUSB0", baudrate=115200, timeout=2.0)
            if not success:
                return jsonify({"success": False, "error": "Device not connected. Please verify connection first."}), 400
            logger.info(f"Auto-connected to K6 {version}")
        
        # Clear cancel flag
        global burn_cancel_event
        burn_cancel_event.clear()
        
        job_data = request.get_json()
        job = job_data.get("job", {})
        
        # Extract burn jobs
        burn_jobs = job.get("burn_jobs", {})
        if not burn_jobs:
            return jsonify({"success": False, "error": "No burn jobs defined"}), 400
        
        # Filter enabled jobs
        enabled_jobs = {name: config for name, config in burn_jobs.items() 
                       if config.get("enabled", True)}
        
        if not enabled_jobs:
            return jsonify({"success": False, "error": "No enabled burn jobs"}), 400
        
        results = []
        
        for job_name, job_config in enabled_jobs.items():
            # Decode base64 image
            if "image_data" not in job_config:
                results.append({
                    "job": job_name,
                    "success": False,
                    "error": "No image_data in job config"
                })
                continue
            
            b64_data = job_config["image_data"]
            if b64_data.startswith("data:image/png;base64,"):
                b64_data = b64_data.split(",")[1]
            
            import base64
            img_bytes = base64.b64decode(b64_data)
            
            # Save to temp file
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            temp_path = DATA_DIR / f"job_{job_name}_{timestamp}.png"
            
            with open(temp_path, 'wb') as f:
                f.write(img_bytes)
            
            try:
                # Extract parameters
                power = int(job_config.get("power", 1000))
                depth = int(job_config.get("depth", 100))
                center_x = job_config.get("center_x")
                center_y = job_config.get("center_y")
                
                # Create CSV log
                csv_path = DATA_DIR / f"job_{job_name}_{timestamp}.csv"
                
                emit_progress("start", 0, f"Starting job: {job_name}")
                
                with ProgressCSVLogger(str(csv_path)) as csv_logger:
                    result = k6_service.engrave(
                        str(temp_path),
                        power=power,
                        depth=depth,
                        center_x=center_x,
                        center_y=center_y,
                        csv_logger=csv_logger
                    )
                
                results.append({
                    "job": job_name,
                    "success": result["ok"],
                    "message": result.get("message", ""),
                    "total_time": result.get("total_time", 0),
                    "chunks": result.get("chunks", 0),
                    "csv_log": str(csv_path)
                })
                
                if not result["ok"]:
                    emit_progress("error", 0, f"{job_name} failed: {result['message']}")
                    break  # Stop on first failure
                else:
                    emit_progress("complete", 100, f"{job_name} complete")
                    
            finally:
                temp_path.unlink(missing_ok=True)
        
        # Check if all succeeded
        all_success = all(r["success"] for r in results)
        
        if all_success:
            return jsonify({"success": True, "results": results})
        else:
            failed = [r for r in results if not r["success"]]
            return jsonify({
                "success": False, 
                "error": f"{len(failed)} job(s) failed",
                "results": results
            }), 500
            
    except Exception as e:
        logger.error(f"Job burn failed: {e}")
        emit_progress("error", 0, str(e))
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/burn", methods=["POST"])
def calibration_burn():  # noqa: C901
    """Burn calibration pattern"""
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
            # Bottom-out test: diagonal line in bottom 1/8th of Y-axis work area
            # Full 1600x1600px to test Y-axis limits and find bottom edge cropping
            work_area_px = mm_to_px(80.0)
            
            img = Image.new("1", (work_area_px, work_area_px), 1)
            draw = ImageDraw.Draw(img)
            
            # Calculate bottom 1/8th region (last 200px of 1600px)
            bottom_eighth = work_area_px // 8  # 200px
            start_y = work_area_px - bottom_eighth  # y=1400
            
            # Draw diagonal line from top-left to bottom-right of bottom 1/8th region
            # From (0, 1400) to (1600, 1600)
            draw.line(
                (0, start_y, work_area_px - 1, work_area_px - 1),
                fill=0,
                width=3
            )

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


@app.route("/api/preview", methods=["POST"])
def preview():
    """Generate multi-layer preview for job at any stage
    
    Core of single-step workflow - visual feedback for debugging/testing.
    
    Input:
        job: dict (partial or complete .k6job format)
        stage: str (upload/process/layout/all)
        options: dict (show_layers, show_grid, show_annotations)
    
    Output:
        preview: base64 PNG
        warnings: list of warning messages
    """
    try:
        data = request.get_json()
        job = data.get("job", {})
        stage = data.get("stage", "layout")
        options = data.get("options", {})
        
        # Render preview
        img = PreviewService.render_preview(job, stage, options)
        
        # Convert to base64
        preview_b64 = image_service.image_to_base64(img)
        
        # Detect warnings
        warnings = PreviewService.detect_warnings(job)
        
        return jsonify({
            "success": True,
            "preview": preview_b64,
            "dimensions": {"width": img.width, "height": img.height},
            "warnings": warnings,
            "stage": stage
        })
    
    except Exception as e:
        logger.error(f"Preview generation failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
