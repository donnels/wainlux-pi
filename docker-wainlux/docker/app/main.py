"""
Flask app for K6 laser control

Web interface for Wainlux K6 laser engraver.
Access at http://<pi-ip>:8080
"""

from flask import Flask, render_template, request, jsonify, Response
from werkzeug.exceptions import HTTPException
from pathlib import Path
import logging
import tempfile
import sys
import os
import json
import time
import queue
import threading
from typing import Optional
from uuid import uuid4
from PIL import Image, ImageDraw

# Add k6 library to path (located in /app/k6 in container)
# From /app/app/main.py, parent is /app/app, parent.parent is /app
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # Add /app/app for services module

from k6.csv_logger import CSVLogger
from k6.commands import K6CommandBuilder, CommandSequence
from services import ImageService, K6Service, QRService, PatternService, PipelineService, PreviewService
from services.k6_service import K6DeviceManager
from constants import K6Constants
from utils import safe_int, safe_float, parse_bool, file_timestamp, iso_timestamp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.secret_key = 'k6-laser-session-key-change-in-production'


@app.errorhandler(Exception)
def handle_unexpected_error(err):
    """Ensure API endpoints always return JSON, even for unhandled failures."""
    if request.path.startswith("/api/"):
        status = 500
        message = str(err) or "Internal Server Error"
        if isinstance(err, HTTPException):
            status = err.code or 500
            message = err.description or message
        if status >= 500:
            logger.exception("Unhandled API error on %s: %s", request.path, err)
        return jsonify({"success": False, "error": message}), status

    if isinstance(err, HTTPException):
        return err
    logger.exception("Unhandled web error on %s: %s", request.path, err)
    return "Internal Server Error", 500

# Device manager and services
device_manager = K6DeviceManager()
k6_service = K6Service(device_manager)
image_service = ImageService()
qr_service = QRService()

# Operational mode (workflow testing): silent (default) / verbose / single-step
# Controls logging and intermediate file generation for debugging/learning
# Request-scoped mode defaults (client may override per request via header/query).
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
    """Get request-scoped operation mode."""
    requested = request.headers.get("X-K6-Operation-Mode") or request.args.get("operation_mode")
    if isinstance(requested, str) and requested in VALID_MODES:
        return requested
    return DEFAULT_MODE

def set_operation_mode(mode: str):
    """Validate operation mode (stateless: no server-side persistence)."""
    return mode in VALID_MODES

def get_dry_run():
    """Get request-scoped dry-run preference."""
    requested = request.headers.get("X-K6-Dry-Run")
    if requested is None:
        requested = request.args.get("dry_run")
    if requested is None:
        return DEFAULT_DRY_RUN
    return parse_bool(requested)

def set_dry_run(enabled: bool):
    """Validate dry run setting (stateless: no server-side persistence)."""
    bool(enabled)
    return True


def _json_payload() -> dict:
    """Return JSON object payload or raise ValueError."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Invalid JSON payload")
    return data


def _safe_data_path(path_str: str, require_exists: bool = True) -> Path:
    """Resolve and validate a path inside DATA_DIR."""
    if not path_str:
        raise ValueError("Path is required")
    path = Path(path_str).resolve()
    data_root = DATA_DIR.resolve()
    if data_root not in path.parents and path != data_root:
        raise ValueError("Path must be inside data directory")
    if require_exists and not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    return path


def _safe_log_path(path_str: str) -> Path:
    """Resolve and validate a CSV/log path inside DATA_DIR."""
    return _safe_data_path(path_str, require_exists=True)

# Progress tracking for SSE (pub/sub broadcast model)
PROGRESS_SUBSCRIBER_MAX = 200
progress_subscribers: set[queue.Queue] = set()
progress_subscribers_lock = threading.Lock()
burn_in_progress = False
burn_cancel_event = threading.Event()  # Signal to cancel active burn

# Data directory for logs
DATA_DIR = Path(__file__).parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True)


def _ensure_device_connected(auto_connect: bool = True) -> None:
    """Ensure device transport is ready; optionally auto-connect."""
    if device_manager.is_connected() and device_manager.transport is not None:
        return
    if not auto_connect:
        raise RuntimeError("Device not connected")

    logger.info("Device not connected, attempting auto-connect...")
    success, version = device_manager.connect(port="/dev/ttyUSB0", baudrate=115200, timeout=2.0)
    if not success or device_manager.transport is None:
        raise RuntimeError("Device not connected. Please verify connection first.")
    logger.info(f"Auto-connected to K6 {version}")


# K6 hardware constants (shared single source of truth)
K6_MAX_WIDTH = K6Constants.MAX_WIDTH_PX
K6_MAX_HEIGHT = K6Constants.MAX_HEIGHT_PX
K6_CENTER_X_OFFSET = K6Constants.CENTER_X_OFFSET_PX
K6_CENTER_Y = K6Constants.CENTER_Y_PX
K6_DEFAULT_CENTER_X = K6Constants.DEFAULT_CENTER_X_PX
K6_DEFAULT_CENTER_Y = K6Constants.DEFAULT_CENTER_Y_PX
MAX_EMBED_IMAGE_DIM = K6Constants.MAX_WIDTH_PX

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "gif"}


def _normalize_embed_image(
    image: Image.Image,
    max_dimension: int = MAX_EMBED_IMAGE_DIM,
) -> tuple[Image.Image, dict]:
    """Normalize image for embedded base64 payloads.

    - Preserves aspect ratio
    - Limits longest edge to max_dimension
    - Converts to 1-bit to avoid gray artifacts in burn pipeline
    """
    gray = image.convert("L")
    original_width, original_height = gray.size
    resized = False

    if max(original_width, original_height) > max_dimension:
        scale = max_dimension / float(max(original_width, original_height))
        target_width = max(1, int(round(original_width * scale)))
        target_height = max(1, int(round(original_height * scale)))
        gray = gray.resize((target_width, target_height), Image.LANCZOS)
        resized = True

    normalized = gray.point(lambda x: 0 if x < 128 else 255, mode="1")

    return normalized, {
        "original_width": original_width,
        "original_height": original_height,
        "width": normalized.width,
        "height": normalized.height,
        "resized": resized,
        "max_dimension": max_dimension,
    }


def emit_progress(phase: str, progress: int, message: str):
    """Emit a progress event to all SSE listeners."""
    event = {
        "phase": phase,
        "progress": progress,
        "message": message,
        "timestamp": iso_timestamp(),
    }
    logger.info(f"EMIT_PROGRESS: phase={phase}, progress={progress}, message={message}")
    dropped = 0
    delivered = 0
    with progress_subscribers_lock:
        subscribers = list(progress_subscribers)
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(event)
            delivered += 1
        except queue.Full:
            dropped += 1
    logger.info(f"EMIT_PROGRESS: delivered={delivered}, dropped={dropped}")


def _run_burn_with_csv(
    *,
    image_path: str,
    power: int,
    depth: int,
    center_x: Optional[int],
    center_y: Optional[int],
    csv_prefix: str,
    job_id: str,
    dry_run_override: Optional[bool] = None,
    start_message: Optional[str] = None,
    setup_message: Optional[str] = None,
    complete_message_template: Optional[str] = None,
    emit_error: bool = True,
) -> tuple[dict, int]:
    """Execute one burn via pipeline stages (process/build/execute) with CSV logging."""
    timestamp = file_timestamp()
    run_id = str(uuid4())
    csv_path = DATA_DIR / f"{csv_prefix}-{timestamp}.csv"
    process_result = None
    build_result = None
    keep_pipeline_artifacts = False

    _ensure_device_connected(auto_connect=True)

    if start_message:
        emit_progress("start", 0, start_message)

    try:
        if setup_message:
            emit_progress("setup", 0, setup_message)

        emit_progress("prepare", 10, "Pipeline step 2/4: processing image")
        process_result = PipelineService.process_image(
            image_path,
            DATA_DIR,
            threshold=128,
            invert=False,
        )

        resolved_center_x = center_x
        resolved_center_y = center_y
        if resolved_center_x is None:
            resolved_center_x = (process_result["width"] // 2) + K6_CENTER_X_OFFSET
        if resolved_center_y is None:
            resolved_center_y = process_result["height"] // 2

        emit_progress("prepare", 45, "Pipeline step 3/4: building command sequence")
        build_result = PipelineService.build_commands(
            process_result["processed_path"],
            power,
            depth,
            resolved_center_x,
            resolved_center_y,
            DATA_DIR,
        )

        emit_progress("upload", 70, "Pipeline step 4/4: executing commands")
        with ProgressCSVLogger(str(csv_path), run_id=run_id, job_id=job_id) as csv_logger:
            previous_dry_run = device_manager.dry_run
            if dry_run_override is not None:
                device_manager.dry_run = bool(dry_run_override)
            started = time.time()
            try:
                with device_manager.serial_lock:
                    result = device_manager.driver.execute_from_file(
                        device_manager.transport,
                        build_result["command_path"],
                        csv_logger=csv_logger,
                    )
            finally:
                elapsed = time.time() - started
                if dry_run_override is not None:
                    device_manager.dry_run = previous_dry_run
    except (ValueError, RuntimeError, OSError) as exc:
        if emit_error:
            emit_progress("error", 0, str(exc))
        return {"success": False, "error": str(exc)}, 500
    finally:
        mode = get_operation_mode()
        keep_pipeline_artifacts = mode in {"verbose", "single-step"}
        if not keep_pipeline_artifacts and process_result and build_result:
            for key in ("processed_path", "preview_path"):
                path = process_result.get(key)
                if path:
                    Path(path).unlink(missing_ok=True)
            for key in ("command_path", "metadata_path"):
                path = build_result.get(key)
                if path:
                    Path(path).unlink(missing_ok=True)

    if burn_cancel_event.is_set():
        emit_progress("stopped", 0, "Burn cancelled by user")
        return {"success": False, "error": "Cancelled by user"}, 400

    if result.get("ok"):
        chunk_count = 0
        if build_result:
            chunk_count = build_result.get("sequence", {}).get("data_chunks", 0)
        if complete_message_template:
            emit_progress("complete", 100, complete_message_template.format(total_time=elapsed))
        return {
            "success": True,
            "message": "Burn complete",
            "total_time": round(elapsed, 3),
            "chunks": chunk_count,
            "csv_log": str(csv_path),
            "run_id": run_id,
            "command_path": build_result.get("command_path") if (build_result and keep_pipeline_artifacts) else None,
            "metadata_path": build_result.get("metadata_path") if (build_result and keep_pipeline_artifacts) else None,
        }, 200

    if emit_error:
        emit_progress("error", 0, result.get("error", "Pipeline execution failed"))
    return {"success": False, "error": result.get("error", "Pipeline execution failed")}, 500


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
    except (OSError, json.JSONDecodeError) as e:
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
    except (OSError, json.JSONDecodeError) as e:
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
    except OSError as e:
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
    except (RuntimeError, OSError) as e:
        logger.error(f"Connect failed: {e}")
        return jsonify({"success": False, "connected": False, "error": str(e)}), 500


@app.route("/api/test/bounds", methods=["POST"])
def test_bounds():
    try:
        success = k6_service.draw_bounds()
        return jsonify({"success": success})
    except (RuntimeError, OSError) as e:
        logger.error(f"Bounds test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/test/home", methods=["POST"])
def test_home():
    try:
        success = k6_service.home()
        return jsonify({"success": success})
    except (RuntimeError, OSError) as e:
        logger.error(f"Home test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/jog", methods=["POST"])
def jog():
    """Move laser head to absolute position using BOUNDS positioning"""
    try:
        data = _json_payload()
        center_x = safe_int(data.get("x", 800), "x")
        center_y = safe_int(data.get("y", 800), "y")
        disable_limits = parse_bool(data.get("disable_limits", False))

        success, result = k6_service.jog(center_x, center_y, disable_limits)

        if success:
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": "BOUNDS command failed"}), 500

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Jog failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mark", methods=["POST"])
def mark():
    """Burn a single pixel mark at current position"""
    try:
        data = _json_payload()
        center_x = safe_int(data.get("x", K6_DEFAULT_CENTER_X), "x")
        center_y = safe_int(data.get("y", K6_DEFAULT_CENTER_Y), "y")
        power = safe_int(data.get("power", 500), "power", 0, 1000)
        depth = safe_int(data.get("depth", 10), "depth", 1, 255)

        success, result = k6_service.mark(center_x, center_y, power, depth)

        if success:
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": "Mark failed"}), 500

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Mark failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/crosshair", methods=["POST"])
def crosshair():
    """Toggle crosshair/positioning laser (0x06 ON, 0x07 OFF)"""
    try:
        data = _json_payload()
        enable = parse_bool(data.get("enable", False))

        success, result = k6_service.crosshair(enable)

        if success:
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": "Crosshair command failed"}), 500

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
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
        timestamp = file_timestamp()
        temp_path = DATA_DIR / f"upload_{timestamp}.png"
        original_filename = file.filename  # Metadata only (for logging)
        source = str(request.form.get("source", "upload") or "upload")
        source_reference = str(request.form.get("source_reference", original_filename) or original_filename)
        
        # Detect SVG by content (XML declaration or <svg tag)
        file_content = file.read()
        file.seek(0)  # Reset for save
        
        file_size_bytes = len(file_content)
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
            normalized_img, image_meta = _normalize_embed_image(img, max_dimension=MAX_EMBED_IMAGE_DIM)
            # Save normalized image to temp file for downstream burn path
            normalized_img.save(str(temp_path))
            # Generate base64 (embedded source of truth for job structure)
            data_url = image_service.image_to_base64(normalized_img)

        return jsonify({
            "success": True,
            "image": data_url,  # For immediate preview display
            "image_base64": data_url,  # Embedded source of truth for job structure
            "temp_path": str(temp_path),
            "original_filename": original_filename,  # Track original name
            "source": source,
            "source_reference": source_reference,
            "file_size_bytes": file_size_bytes,
            "mime_type": file.mimetype or "application/octet-stream",
            "width": image_meta["width"],
            "height": image_meta["height"],
            "original_width": image_meta["original_width"],
            "original_height": image_meta["original_height"],
            "resized": image_meta["resized"],
            "max_dimension": image_meta["max_dimension"],
        })

    except (ValueError, OSError) as e:
        logger.error(f"Engrave prepare failed: {e}")
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/engrave", methods=["POST"])
def engrave():
    """Burn image - mode-aware workflow
    
    Respects request-scoped dry_run and operation mode settings.
    Execution path is pipeline-backed (process/build/execute).
    """
    try:
        _ensure_device_connected(auto_connect=True)

        # Accept both JSON (with temp_path) and FormData (direct upload)
        data = _json_payload()
        temp_path = data.get("temp_path", "")

        # SAFETY: request-level dry_run must override session defaults.
        dry_run_input = data.get("dry_run", None)
        if dry_run_input is None and "dry_run" in request.form:
            dry_run_input = request.form.get("dry_run")
        effective_dry_run = get_dry_run() if dry_run_input is None else parse_bool(dry_run_input)
        
        # Fallback: if no temp_path, accept direct file upload (backward compat)
        if temp_path:
            try:
                temp_path = str(_safe_data_path(temp_path, require_exists=True))
            except (ValueError, FileNotFoundError) as e:
                return jsonify({"success": False, "error": str(e)}), 400
        elif "image" in request.files:
            file = request.files["image"]
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                file.save(tmp.name)
                temp_path = tmp.name
        
        if not temp_path or not Path(temp_path).exists():
            return jsonify({"success": False, "error": "No image file"}), 400

        # Get parameters
        depth = safe_int(data.get("depth", 100), "depth", 1, 255)
        power = safe_int(data.get("power", 1000), "power", 0, 1000)
        
        # Positioning: Use provided coordinates or let driver auto-calculate
        # Driver auto-calc: center_x = (width // 2) + 67, center_y = height // 2
        center_x = data.get("center_x", None)
        center_y = data.get("center_y", None)
        
        if center_x is not None:
            center_x = safe_int(center_x, "center_x")
        if center_y is not None:
            center_y = safe_int(center_y, "center_y")

        # Clear cancel flag
        global burn_cancel_event
        burn_cancel_event.clear()
        
        logger.info(f"=== ENGRAVE START === temp_path={temp_path}, power={power}, depth={depth}")
        logger.info(f"Device connected: {device_manager.is_connected()}")
        
        response, status_code = _run_burn_with_csv(
            image_path=temp_path,
            power=power,
            depth=depth,
            center_x=center_x,
            center_y=center_y,
            csv_prefix="burn",
            job_id="engrave",
            dry_run_override=effective_dry_run,
            start_message="Starting image burn",
            setup_message="Initializing burn sequence...",
            complete_message_template="Burn complete in {total_time:.1f}s",
            emit_error=True,
        )
        
        # Clean up temp file
        Path(temp_path).unlink(missing_ok=True)
        
        if status_code == 200:
            response["dry_run"] = effective_dry_run
        return jsonify(response), status_code

    except KeyboardInterrupt:
        logger.info("Burn cancelled by user")
        emit_progress("stopped", 0, "Burn cancelled")
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Cancelled by user"}), 400
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
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
        data = _json_payload()
        temp_path = data.get("temp_path", "")
        threshold = safe_int(data.get("threshold", 128), "threshold", 0, 255)
        invert = parse_bool(data.get("invert", False))

        try:
            temp_path = str(_safe_data_path(temp_path, require_exists=True))
        except (ValueError, FileNotFoundError) as e:
            return jsonify({"success": False, "error": str(e)}), 400

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

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Pipeline process failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/pipeline/build", methods=["POST"])
def pipeline_build():
    """Build command sequence from processed data - pipeline step 3
    
    Unix pipe model: processed.npy | build → commands.bin + metadata.json
    Exposes all settings (power, depth, position) and command statistics
    """
    try:
        data = _json_payload()
        processed_path = data.get("processed_path", "")
        power = safe_int(data.get("power", 1000), "power", 0, 1000)
        depth = safe_int(data.get("depth", 100), "depth", 1, 255)
        center_x = safe_int(data.get("center_x", K6_DEFAULT_CENTER_X), "center_x")
        center_y = safe_int(data.get("center_y", K6_DEFAULT_CENTER_Y), "center_y")
        
        try:
            processed_path = str(_safe_data_path(processed_path, require_exists=True))
        except (ValueError, FileNotFoundError) as e:
            return jsonify({"success": False, "error": str(e)}), 400

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
    except (RuntimeError, OSError) as e:
        logger.error(f"Pipeline build failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/pipeline/execute", methods=["POST"])
def pipeline_execute():
    """Execute pre-built command sequence - pipeline step 4
    
    Unix pipe model: commands.bin | execute | tee logs
    Raw byte logging (like `tee`) - captures ALL I/O for debugging
    """
    try:
        data = _json_payload()
        command_path = data.get("command_path", "")
        log_mode = data.get("log_mode", "both")  # "csv" | "bytes" | "both"

        try:
            command_path = str(_safe_data_path(command_path, require_exists=True))
        except (ValueError, FileNotFoundError) as e:
            return jsonify({"success": False, "error": str(e)}), 400

        try:
            _ensure_device_connected(auto_connect=False)
        except RuntimeError as e:
            return jsonify({"success": False, "error": str(e)}), 400

        # Setup logging
        timestamp = file_timestamp()
        run_id = str(uuid4())
        
        csv_logger = None
        byte_logger = None
        
        if log_mode in ("csv", "both"):
            from k6.csv_logger import CSVLogger
            csv_path = DATA_DIR / f"execute_{timestamp}.csv"
            csv_logger = CSVLogger(str(csv_path), run_id=run_id, job_id="pipeline_execute")
        
        if log_mode in ("bytes", "both"):
            from k6.byte_logger import ByteDumpLogger
            byte_path = DATA_DIR / f"execute_{timestamp}"
            byte_logger = ByteDumpLogger(str(byte_path))

        try:
            # Execute commands with logging
            with device_manager.serial_lock:
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
                response["run_id"] = run_id
            if byte_logger:
                response["byte_dump"] = str(byte_path) + ".dump"
                response["byte_dump_text"] = str(byte_path) + ".dump.txt"

            return jsonify(response)

        finally:
            if csv_logger:
                csv_logger.close()
            if byte_logger:
                byte_logger.close()

    except (RuntimeError, OSError) as e:
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
        "resolution_mm_per_px": K6Constants.RESOLUTION_MM_PER_PX,
        "center_x_offset_px": K6Constants.CENTER_X_OFFSET_PX,
        "center_y_px": K6Constants.CENTER_Y_PX,
        "default_center_x_px": K6Constants.DEFAULT_CENTER_X_PX,
        "default_center_y_px": K6Constants.DEFAULT_CENTER_Y_PX,
        "work_width_mm": K6Constants.WORK_WIDTH_MM,
        "work_height_mm": K6Constants.WORK_HEIGHT_MM,
    }
    version = device_manager.version
    if version:
        # device_manager.version may already be a string (mock) or a tuple
        if isinstance(version, (tuple, list)) and len(version) >= 3:
            response["version"] = f"v{version[0]}.{version[1]}.{version[2]}"
        else:
            response["version"] = str(version)
    
    # Request-scoped settings (stateless semantics)
    response["operation_mode"] = get_operation_mode()
    response["dry_run"] = get_dry_run()
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
    """Get request-scoped settings (stateless server)."""
    return jsonify({
        "success": True,
        "operation_mode": get_operation_mode(),
        "dry_run": get_dry_run(),
        "valid_modes": list(VALID_MODES),
        "persisted": False,
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Validate request-scoped settings (stateless, no server-side persistence)."""
    try:
        data = _json_payload()

        effective_mode = get_operation_mode()
        effective_dry_run = get_dry_run()

        if "operation_mode" in data:
            mode = data["operation_mode"]
            if not set_operation_mode(mode):
                return jsonify({"success": False, "error": f"Invalid mode: {mode}"}), 400
            effective_mode = mode

        if "dry_run" in data:
            dry_run_enabled = parse_bool(data["dry_run"])
            set_dry_run(dry_run_enabled)
            effective_dry_run = dry_run_enabled

        return jsonify({
            "success": True,
            "operation_mode": effective_mode,
            "dry_run": effective_dry_run,
            "persisted": False,
            "message": "Settings are request-scoped. Client should include them per request.",
        })
    
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Settings update failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/progress/stream")
def progress_stream():
    """SSE endpoint for real-time progress updates."""

    def generate():
        client_queue = queue.Queue(maxsize=PROGRESS_SUBSCRIBER_MAX)
        with progress_subscribers_lock:
            progress_subscribers.add(client_queue)

        # Send initial connection event
        initial_event = {
            "phase": "connected",
            "progress": 0,
            "message": "Stream connected",
        }
        yield f"data: {json.dumps(initial_event)}\n\n"

        try:
            while True:
                try:
                    # Wait for events with timeout
                    event = client_queue.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Send keepalive
                    yield ": keepalive\n\n"
        finally:
            with progress_subscribers_lock:
                progress_subscribers.discard(client_queue)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/calibration/generate", methods=["POST"])
def calibration_generate():
    """Generate calibration test pattern - unified workflow step 1"""
    try:
        data = _json_payload()
        resolution = safe_float(data.get("resolution", K6Constants.RESOLUTION_MM_PER_PX), "resolution", 0.001)
        pattern = data.get("pattern", "center")
        size_mm = safe_float(data.get("size_mm", 10.0), "size_mm", 0.1)

        # Generate pattern using service (DRY - single source)
        img = PatternService.generate(pattern, resolution, size_mm)

        # Save to temp file
        timestamp = file_timestamp()
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

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Calibration generate failed: {e}")
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/preview", methods=["POST"])
def calibration_preview():
    """Draw preview bounds using fast 0x20 BOUNDS command"""
    try:
        data = _json_payload()
        width = safe_int(data.get("width", K6_MAX_WIDTH), "width", 1, K6_MAX_WIDTH)
        height = safe_int(data.get("height", K6_MAX_HEIGHT), "height", 1, K6_MAX_HEIGHT)

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

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Calibration preview failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/burn-bounds", methods=["POST"])
def calibration_burn_bounds():
    """Burn bounds frame - creates physical positioning guide"""
    try:
        data = _json_payload()
        width = safe_int(data.get("width", K6_MAX_WIDTH), "width", 1, K6_MAX_WIDTH)
        height = safe_int(data.get("height", K6_MAX_HEIGHT), "height", 1, K6_MAX_HEIGHT)
        power = safe_int(data.get("power", 500), "power", 0, 1000)
        depth = safe_int(data.get("depth", 10), "depth", 1, 255)

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
            response, status_code = _run_burn_with_csv(
                image_path=tmp_path,
                power=power,
                depth=depth,
                center_x=(width // 2) + K6_CENTER_X_OFFSET,
                center_y=height // 2,
                csv_prefix="calibration-bounds",
                job_id="calibration_bounds",
                setup_message="Preparing bounds frame burn",
                complete_message_template=None,
                emit_error=True,
            )

            if status_code == 200:
                return jsonify(
                    {
                        "success": True,
                        "message": "Bounds frame burned",
                        "width": width,
                        "height": height,
                        "power": power,
                        "depth": depth,
                        "total_time": response.get("total_time", 0),
                        "chunks": response.get("chunks", 0),
                        "csv_log": response.get("csv_log"),
                        "run_id": response.get("run_id"),
                    }
                )
            else:
                return jsonify({"success": False, "error": response.get("error", "Bounds burn failed")}), status_code
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Burn bounds failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/set-speed-power", methods=["POST"])
def calibration_set_speed_power():
    """Test SET_SPEED_POWER (0x25) command - no observable effect expected"""
    try:
        data = _json_payload()
        speed = safe_int(data.get("speed", 115), "speed", 0, 65535)
        power = safe_int(data.get("power", 1000), "power", 0, 1000)

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

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Set speed/power failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/calibration/set-focus-angle", methods=["POST"])
def calibration_set_focus_angle():
    """Test SET_FOCUS_ANGLE (0x28) command - no observable effect expected"""
    try:
        data = _json_payload()
        focus = safe_int(data.get("focus", 20), "focus", 0, 200)
        angle = safe_int(data.get("angle", 0), "angle", 0, 255)

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

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Set focus/angle failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/lab/raw/send", methods=["POST"])
def lab_raw_send():
    """Raw serial send for protocol experiments (MCP/API lab mode)."""
    try:
        data = _json_payload()

        packet_hex = str(data.get("packet_hex", "")).strip()
        timeout = safe_float(data.get("timeout", 2.0), "timeout", 0.05, 30.0)
        read_size = safe_int(data.get("read_size", 256), "read_size", 1, 4096)
        flush_input = parse_bool(data.get("flush_input", False))
        settle_ms = safe_int(data.get("settle_ms", 120), "settle_ms", 10, 1000)
        connect_if_needed = parse_bool(data.get("connect_if_needed", True))
        save_logs = parse_bool(data.get("save_logs", True))

        if not packet_hex:
            return jsonify({"success": False, "error": "packet_hex is required"}), 400

        if connect_if_needed and not device_manager.is_connected():
            success, _ = device_manager.connect(port="/dev/ttyUSB0", baudrate=115200, timeout=2.0)
            if not success:
                return jsonify({"success": False, "error": "Unable to connect to K6 device"}), 500

        if not device_manager.is_connected():
            return jsonify({"success": False, "error": "Device not connected"}), 400

        payload = K6Service.parse_hex_bytes(packet_hex)
        log_prefix = str(DATA_DIR / "raw_io") if save_logs else None

        ok, result = k6_service.raw_send(
            payload=payload,
            timeout=timeout,
            read_size=read_size,
            flush_input=flush_input,
            settle_ms=settle_ms,
            log_prefix=log_prefix,
        )
        if ok:
            return jsonify(result)
        return jsonify(result), 500
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Raw send failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/generate", methods=["POST"])
def qr_generate():
    """Generate WiFi QR code ONCE - save to temp file and return preview"""
    try:
        data = _json_payload()
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        security = data.get("security", "WPA")
        description = data.get("description", "")
        show_password = parse_bool(data.get("show_password", False))

        if not ssid:
            return jsonify({"success": False, "error": "SSID required"}), 400

        # Generate QR image ONCE using QRService
        canvas, metadata = qr_service.generate_wifi_qr(ssid, password, security, description, show_password)
        
        # Save to temp file (will be used by burn endpoint)
        timestamp = file_timestamp()
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

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
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

        data = _json_payload()
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        security = data.get("security", "WPA")
        description = data.get("description", "")
        power = safe_int(data.get("power", 500), "power", 0, 1000)
        depth = safe_int(data.get("depth", 10), "depth", 1, 255)

        if not ssid:
            return jsonify({"success": False, "error": "SSID required"}), 400

        # Validate
        power = _clamp_int(power, 0, 1000)
        depth = _clamp_int(depth, 1, 255)

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

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"QR preview failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/burn", methods=["POST"])
def qr_burn():
    """Burn WiFi QR code using previously generated image"""
    try:
        # Clear cancel flag
        global burn_cancel_event
        burn_cancel_event.clear()

        data = _json_payload()
        temp_path = data.get("temp_path", "")
        ssid = data.get("ssid", "")  # For logging/metadata
        power = safe_int(data.get("power", 500), "power", 0, 1000)
        depth = safe_int(data.get("depth", 10), "depth", 1, 255)

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
            timestamp = file_timestamp()
            temp_path = str(DATA_DIR / f"qr_ondemand_{timestamp}.png")
            canvas.save(temp_path)

        # Validate
        power = _clamp_int(power, 0, 1000)
        depth = _clamp_int(depth, 1, 255)

        # Use the temp_path from preview (image already generated)
        logger.info(f"Using pre-generated QR image: {temp_path}")

        # Get dimensions from the image
        with Image.open(temp_path) as img:
            burn_width, burn_height = img.size

        emit_progress("start", 0, "Starting WiFi QR burn")
        emit_progress("prepare", 10, f"SSID: {ssid}, {burn_width}x{burn_height}px")

        try:
            # Position at center (default for left-aligned card)
            center_x = (burn_width // 2) + K6_CENTER_X_OFFSET
            center_y = K6_DEFAULT_CENTER_Y

            response, status_code = _run_burn_with_csv(
                image_path=temp_path,
                power=power,
                depth=depth,
                center_x=center_x,
                center_y=center_y,
                csv_prefix="qr-wifi",
                job_id="qr_wifi",
                setup_message="Starting burn sequence...",
                complete_message_template="Burn complete in {total_time:.1f}s",
                emit_error=True,
            )
            return jsonify(response), status_code

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
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"QR burn failed: {e}")
        emit_progress("error", 0, str(e))
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/qr/alignment", methods=["POST"])
def qr_alignment():
    """Burn alignment box for credit card positioning"""
    try:
        data = _json_payload()
        power = safe_int(data.get("power", 300), "power", 0, 1000)
        depth = safe_int(data.get("depth", 5), "depth", 1, 255)

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
            # Position at center
            center_x = (burn_width // 2) + K6_CENTER_X_OFFSET
            center_y = K6_DEFAULT_CENTER_Y

            response, status_code = _run_burn_with_csv(
                image_path=tmp_path,
                power=power,
                depth=depth,
                center_x=center_x,
                center_y=center_y,
                csv_prefix="qr-alignment",
                job_id="qr_alignment",
                complete_message_template=None,
                emit_error=False,
            )
            if status_code == 200:
                response["message"] = "Alignment box burned"
            return jsonify(response), status_code

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
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
        data = _json_payload()
        width_mm = safe_float(data.get("width_mm", 50.0), "width_mm", 0.1)
        height_mm = safe_float(data.get("height_mm", 30.0), "height_mm", 0.1)
        align_x = data.get("align_x", "center")  # left/right/center
        align_y = data.get("align_y", "center")  # top/bottom/center
        burn_offset_x = safe_float(data.get("burn_offset_x", 0.0), "burn_offset_x")
        burn_offset_y = safe_float(data.get("burn_offset_y", 0.0), "burn_offset_y")
        resolution = safe_float(data.get("resolution", K6Constants.RESOLUTION_MM_PER_PX), "resolution", 0.001)
        
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
        timestamp = file_timestamp()
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
    except (RuntimeError, OSError) as e:
        logger.error(f"Alignment builder failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _decode_data_url_image(data_url: str) -> Image.Image:
    """Decode data URL/base64 image into a PIL image."""
    import base64
    import io

    b64_data = data_url
    if data_url.startswith("data:image"):
        b64_data = data_url.split(",", 1)[1]
    raw = base64.b64decode(b64_data)
    return Image.open(io.BytesIO(raw))


def _get_layout_pixels(job: dict) -> tuple[float, float, float, float]:
    """Return material center and image offset in pixels from job layout."""
    layout = job.get("layout", {})
    material_layout = layout.get("material_on_burn_area", {})
    image_layout = layout.get("image_on_material", {})

    mm_per_px = K6Constants.RESOLUTION_MM_PER_PX
    material_center_x_px = float(material_layout.get("center_x_mm", 40.0)) / mm_per_px
    material_center_y_px = float(material_layout.get("center_y_mm", 38.0)) / mm_per_px
    image_offset_x_px = float(image_layout.get("center_x_mm", 0.0)) / mm_per_px
    image_offset_y_px = float(image_layout.get("center_y_mm", 0.0)) / mm_per_px

    return material_center_x_px, material_center_y_px, image_offset_x_px, image_offset_y_px


def _render_job_image(job: dict, job_config: dict) -> Image.Image:
    """Render a burn image from job metadata (image/bounds/combined)."""
    render_spec = job_config.get("render_spec", {})
    mode = render_spec.get("mode", "image")

    upload_data = job.get("stages", {}).get("upload", {}).get("data", {})
    image_data = upload_data.get("image_base64") or upload_data.get("image")

    if not image_data:
        raise ValueError("No source image available in job.stages.upload.data")

    source_img = _decode_data_url_image(image_data)

    if mode == "image":
        # Keep source untouched; driver pipeline normalizes to burn format.
        return source_img

    material = job.get("material", {})
    target = material.get("target", {})
    target_width_mm = float(target.get("width_mm", 0))
    target_height_mm = float(target.get("height_mm", 0))
    if target_width_mm <= 0 or target_height_mm <= 0:
        raise ValueError("Target object dimensions required for bounds/combined modes")

    shape_width_px = int(round(target_width_mm / K6Constants.RESOLUTION_MM_PER_PX))
    shape_height_px = int(round(target_height_mm / K6Constants.RESOLUTION_MM_PER_PX))
    burn_width_px = min(shape_width_px, K6_MAX_WIDTH)
    burn_height_px = min(shape_height_px, K6_MAX_HEIGHT)
    border_width = safe_int(render_spec.get("border_width", 3), "border_width", 1, 10)

    canvas = Image.new("1", (burn_width_px, burn_height_px), 1)
    draw = ImageDraw.Draw(canvas)

    # Rectangle centered on burn canvas; overflow is clipped intentionally.
    obj_x = (burn_width_px - shape_width_px) / 2
    obj_y = (burn_height_px - shape_height_px) / 2
    margin = border_width / 2
    draw.rectangle(
        (
            obj_x + margin,
            obj_y + margin,
            obj_x + shape_width_px - border_width,
            obj_y + shape_height_px - border_width,
        ),
        outline=0,
        width=border_width,
    )

    if mode == "bounds":
        return canvas

    if mode == "combined":
        _, _, image_offset_x_px, image_offset_y_px = _get_layout_pixels(job)
        source_bw = source_img.convert("1")
        img_x = int(round((burn_width_px / 2) + image_offset_x_px - (source_bw.width / 2)))
        img_y = int(round((burn_height_px / 2) + image_offset_y_px - (source_bw.height / 2)))
        canvas.paste(source_bw, (img_x, img_y))
        return canvas

    raise ValueError(f"Unknown render_spec mode: {mode}")


def _resolve_job_center(job: dict, job_config: dict) -> tuple[Optional[int], Optional[int]]:
    """Resolve burn center from explicit values or strategy."""
    center_x = job_config.get("center_x")
    center_y = job_config.get("center_y")
    if center_x is not None and center_y is not None:
        return int(center_x), int(center_y)

    strategy = job_config.get("center_strategy", "auto")
    material_center_x_px, material_center_y_px, image_offset_x_px, image_offset_y_px = _get_layout_pixels(job)

    if strategy == "material_only":
        return int(round(material_center_x_px + K6_CENTER_X_OFFSET)), int(round(material_center_y_px))
    if strategy == "material_plus_image":
        return (
            int(round(material_center_x_px + image_offset_x_px + K6_CENTER_X_OFFSET)),
            int(round(material_center_y_px + image_offset_y_px)),
        )
    return None, None


@app.route("/api/reports/summary", methods=["POST"])
def report_summary():
    """Generate structured timing/statistics summary from a CSV log."""
    try:
        import csv

        payload = _json_payload()
        csv_log = payload.get("csv_log", "")
        if not csv_log:
            return jsonify({"success": False, "error": "csv_log is required"}), 400

        csv_path = _safe_log_path(csv_log)
        rows = []
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        if not rows:
            return jsonify({"success": False, "error": "Empty CSV log"}), 400

        def as_float(value, default=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def as_int(value, default=0):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        phase_stats = {}
        response_counts = {}
        op_count = len(rows)
        total_elapsed_s = max(as_float(r.get("elapsed_s", 0.0)) for r in rows)
        total_bytes = max(as_int(r.get("cumulative_bytes", 0)) for r in rows)

        timeline = []
        chunk_timings = []
        status_series = []

        for row in rows:
            phase = row.get("phase", "UNKNOWN")
            operation = row.get("operation", "")
            duration_ms = as_float(row.get("duration_ms", 0))
            elapsed_s = as_float(row.get("elapsed_s", 0))
            bytes_transferred = as_int(row.get("bytes_transferred", 0))
            response_type = row.get("response_type", "") or "NONE"
            status_pct = row.get("status_pct", "")

            phase_entry = phase_stats.setdefault(phase, {"count": 0, "duration_ms": 0.0, "bytes": 0})
            phase_entry["count"] += 1
            phase_entry["duration_ms"] += duration_ms
            phase_entry["bytes"] += bytes_transferred

            response_counts[response_type] = response_counts.get(response_type, 0) + 1

            timeline.append({
                "elapsed_s": round(elapsed_s, 3),
                "phase": phase,
                "operation": operation,
                "duration_ms": round(duration_ms, 2),
                "response_type": response_type,
            })

            if "chunk" in operation.lower():
                chunk_timings.append({
                    "elapsed_s": round(elapsed_s, 3),
                    "duration_ms": round(duration_ms, 2),
                    "operation": operation,
                })

            if status_pct not in ("", None):
                try:
                    pct = int(status_pct)
                    status_series.append({"elapsed_s": round(elapsed_s, 3), "pct": pct})
                except ValueError:
                    pass

        throughput_kbps = 0.0
        if total_elapsed_s > 0:
            throughput_kbps = round((total_bytes / 1024.0) / total_elapsed_s, 3)

        def percentile(values, pct):
            if not values:
                return 0.0
            sorted_vals = sorted(values)
            idx = min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1))))
            return float(sorted_vals[idx])

        chunk_durations = [entry["duration_ms"] for entry in chunk_timings]
        timing_summary = {
            "chunk_count": len(chunk_durations),
            "chunk_avg_ms": round((sum(chunk_durations) / len(chunk_durations)), 2) if chunk_durations else 0.0,
            "chunk_p95_ms": round(percentile(chunk_durations, 95), 2),
            "chunk_max_ms": round(max(chunk_durations), 2) if chunk_durations else 0.0,
            "status_samples": len(status_series),
            "final_status_pct": status_series[-1]["pct"] if status_series else None,
        }

        # Small timeline payload for UI performance on Pi.
        timeline_sample = timeline[::max(1, len(timeline) // 400)]

        first = rows[0]
        report = {
            "success": True,
            "schema_version": first.get("schema_version", "1.x"),
            "run_id": first.get("run_id", ""),
            "job_id": first.get("job_id", ""),
            "burn_start": first.get("burn_start", ""),
            "csv_log": str(csv_path),
            "summary": {
                "operations": op_count,
                "total_elapsed_s": round(total_elapsed_s, 3),
                "total_bytes": total_bytes,
                "avg_throughput_kbps": throughput_kbps,
            },
            "phase_stats": phase_stats,
            "response_counts": response_counts,
            "timing": {
                "status_series": status_series,
                "chunk_timings": chunk_timings,
                "timeline": timeline_sample,
            },
            "timing_summary": timing_summary,
        }
        return jsonify(report)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except OSError as e:
        logger.error(f"Report summary failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/job/preview", methods=["POST"])
def job_preview():
    """Preview rendered burn jobs using same render path as /api/job/burn."""
    try:
        payload = _json_payload()
        job = payload.get("job", {})
        burn_jobs = job.get("burn_jobs", {})
        if not burn_jobs:
            return jsonify({"success": False, "error": "No burn jobs defined"}), 400

        enabled_jobs = {
            name: config for name, config in burn_jobs.items()
            if config.get("enabled", True)
        }
        if not enabled_jobs:
            return jsonify({"success": False, "error": "No enabled burn jobs"}), 400

        previews = []
        for name, config in enabled_jobs.items():
            if "render_spec" in config:
                img = _render_job_image(job, config)
            elif "image_data" in config:
                img = _decode_data_url_image(config["image_data"])
            else:
                raise ValueError(f"No render_spec or image_data in burn job: {name}")

            center_x, center_y = _resolve_job_center(job, config)

            # Lightweight protocol estimate for debug visibility.
            bytes_per_line = (img.width + 7) // 8
            total_data_bytes = bytes_per_line * img.height
            total_chunks = (total_data_bytes + 1899) // 1900
            estimated_commands = 1 + 1 + 2 + total_chunks + 2  # framing+header+connects+data+inits

            previews.append({
                "job": name,
                "render_mode": config.get("render_spec", {}).get("mode", "image_data"),
                "preview_image": image_service.image_to_base64(img),
                "width": img.width,
                "height": img.height,
                "center_x": center_x,
                "center_y": center_y,
                    "power": safe_int(config.get("power", 1000), f"{name}.power", 0, 1000),
                    "depth": safe_int(config.get("depth", 100), f"{name}.depth", 1, 255),
                "estimate": {
                    "bytes_per_line": bytes_per_line,
                    "total_data_bytes": total_data_bytes,
                    "total_chunks": total_chunks,
                    "estimated_commands": estimated_commands,
                },
            })

        warnings = PreviewService.detect_warnings(job)
        return jsonify({
            "success": True,
            "previews": previews,
            "warnings": warnings,
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Job preview failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/job/burn", methods=["POST"])
def job_burn():
    """Burn from complete job JSON structure
    
    Accepts full .k6job JSON with embedded base64 images.
    Processes all enabled burn_jobs in sequence.
    """
    try:
        _ensure_device_connected(auto_connect=True)
        
        # Clear cancel flag
        global burn_cancel_event
        burn_cancel_event.clear()
        
        job_data = _json_payload()
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
        
        dry_run_input = job_data.get("dry_run", None)
        effective_dry_run = get_dry_run() if dry_run_input is None else parse_bool(dry_run_input)

        results = []
        run_id = str(uuid4())
        
        for job_name, job_config in enabled_jobs.items():
            # Save rendered image to temp file
            timestamp = file_timestamp()
            temp_path = DATA_DIR / f"job_{job_name}_{timestamp}.png"

            try:
                # Render from metadata when available; fallback to legacy image_data.
                if "render_spec" in job_config:
                    rendered_img = _render_job_image(job, job_config)
                elif "image_data" in job_config:
                    rendered_img = _decode_data_url_image(job_config["image_data"])
                else:
                    raise ValueError("No render_spec or image_data in job config")

                rendered_img.save(temp_path)

                # Extract parameters
                power = safe_int(job_config.get("power", 1000), f"{job_name}.power", 0, 1000)
                depth = safe_int(job_config.get("depth", 100), f"{job_name}.depth", 1, 255)
                center_x, center_y = _resolve_job_center(job, job_config)
                
                emit_progress("start", 0, f"Starting job: {job_name}")

                response, status_code = _run_burn_with_csv(
                    image_path=str(temp_path),
                    power=power,
                    depth=depth,
                    center_x=center_x,
                    center_y=center_y,
                    csv_prefix=f"job_{job_name}",
                    job_id=job_name,
                    dry_run_override=effective_dry_run,
                    setup_message=f"Initializing job {job_name}",
                    complete_message_template="{total_time:.1f}s",
                    emit_error=False,
                )

                success = status_code == 200 and response.get("success", False)
                results.append({
                    "job": job_name,
                    "success": success,
                    "message": response.get("message", ""),
                    "total_time": response.get("total_time", 0),
                    "chunks": response.get("chunks", 0),
                    "csv_log": response.get("csv_log"),
                    "run_id": response.get("run_id", run_id),
                    "center_x": center_x,
                    "center_y": center_y,
                    "render_mode": job_config.get("render_spec", {}).get("mode", "image_data"),
                })

                if not success:
                    err_msg = response.get("error", response.get("message", "Unknown error"))
                    emit_progress("error", 0, f"{job_name} failed: {err_msg}")
                    break  # Stop on first failure
                else:
                    emit_progress("complete", 100, f"{job_name} complete")
                    
            finally:
                temp_path.unlink(missing_ok=True)
        
        # Check if all succeeded
        all_success = all(r["success"] for r in results)
        
        if all_success:
            return jsonify({"success": True, "run_id": run_id, "results": results})
        else:
            failed = [r for r in results if not r["success"]]
            return jsonify({
                "success": False, 
                "error": f"{len(failed)} job(s) failed",
                "run_id": run_id,
                "results": results
            }), 500
            
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
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
        except (RuntimeError, OSError) as e:
            logger.warning(f"Pre-burn HOME failed: {e}")

        data = _json_payload()
        resolution = safe_float(data.get("resolution", K6Constants.RESOLUTION_MM_PER_PX), "resolution", 0.001)
        pattern = data.get("pattern", "center")
        size_mm = safe_float(data.get("size_mm", 10.0), "size_mm", 0.1)
        power = safe_int(data.get("power", 500), "power", 0, 1000)
        depth = safe_int(data.get("depth", 10), "depth", 1, 255)

        # Validate
        power = _clamp_int(power, 0, 1000)
        depth = _clamp_int(depth, 1, 255)

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
            response, status_code = _run_burn_with_csv(
                image_path=tmp_path,
                power=power,
                depth=depth,
                center_x=center_x,
                center_y=center_y,
                csv_prefix=f"calibration-{pattern}-{resolution}mm",
                job_id=f"calibration_{pattern}",
                setup_message="Image processing complete, starting protocol...",
                complete_message_template="Burn complete in {total_time:.1f}s",
                emit_error=True,
            )
            if status_code == 200:
                response.update(
                    {
                        "pattern": pattern,
                        "resolution": resolution,
                        "size_mm": size_mm,
                        "width_px": img.width,
                        "height_px": img.height,
                        "power": power,
                        "depth": depth,
                    }
                )
            return jsonify(response), status_code

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except KeyboardInterrupt:
        logger.info("Calibration burn cancelled by user")
        emit_progress("stopped", 0, "Burn cancelled")
        if "tmp_path" in locals():
            Path(tmp_path).unlink(missing_ok=True)
        return jsonify({"success": False, "error": "Cancelled by user"}), 400
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
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
        data = _json_payload()
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
    
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except (RuntimeError, OSError) as e:
        logger.error(f"Preview generation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
