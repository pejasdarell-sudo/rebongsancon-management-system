import io
import re
import time
import base64
from PIL import Image, ImageOps
import qrcode
import db

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

DEFAULT_OLED_QR_SIZE = 64  # Fits left 64x64 half of the 128x64 SSD1306 OLED

# Local fallback cache (primary source of truth is Firestore esp32_display_qr/current)
active_esp32_qr = {
    "has_qr": False,
    "qr_id": "",
    "qr_text": "",
    "title": "SYSTEM READY",
    "amount": "",
    "amount_display": "",
    "subtitle": "Standby Mode",
    "width": DEFAULT_OLED_QR_SIZE,
    "height": DEFAULT_OLED_QR_SIZE,
    "invert": True,
    "bitmap_hex": "",
    "preview_url": "",
    "timestamp": "",
    "displayed_on_hardware": False,
    "hardware_ip": "",
    "hardware_ack_at": None,
}


def sanitize_oled_text(text, max_len=20, fallback=""):
    """Normalize text to ASCII-safe characters compatible with Adafruit_GFX 5x7 font."""
    if not text:
        return fallback
    cleaned = str(text).replace("₱", "PHP ").replace("—", "-").replace("–", "-").replace("•", "-")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    ascii_only = cleaned.encode("ascii", "ignore").decode("ascii").strip()
    result = ascii_only[:max_len] if ascii_only else fallback
    return result


def format_amount_ascii(amount_text):
    """Format currency amount for both web UI and 10-char-per-line SSD1306 OLED."""
    if not amount_text:
        return ""
    raw = str(amount_text).strip()
    num_part = raw.replace("₱", "").replace("PHP", "").replace("php", "").strip()
    if not num_part:
        return sanitize_oled_text(raw, max_len=12)
    candidate = f"PHP {num_part}"
    if len(candidate) <= 10:
        return candidate
    compact = f"P{num_part}"
    return compact[:10]


def _compute_otsu_threshold(gray_img):
    """Compute optimal binary threshold using Otsu's method (pure Pillow)."""
    hist = gray_img.histogram()
    total = sum(hist)
    if total == 0:
        return 128

    sum_total = sum(i * hist[i] for i in range(256))
    sum_bg = 0.0
    weight_bg = 0
    max_var = -1.0
    threshold = 128

    for t in range(256):
        weight_bg += hist[t]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += t * hist[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        between_var = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if between_var > max_var:
            max_var = between_var
            threshold = t

    return max(48, min(208, threshold))


def _decode_qr_with_cv2(pil_img):
    """Attempt to extract exact QR payload string using OpenCV if installed."""
    if cv2 is None or np is None:
        return None
    try:
        rgb = np.array(pil_img.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(bgr)
        if data and data.strip():
            return data.strip()

        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        data, _, _ = detector.detectAndDecode(gray)
        if data and data.strip():
            return data.strip()

        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, _, _ = detector.detectAndDecode(thresh)
        if data and data.strip():
            return data.strip()
    except Exception as exc:
        print(f"[QR SERVICE] cv2 decode skipped: {exc}")
    return None


def _smart_crop_qr_region(pil_img):
    """Isolate the square QR code region from screenshots with headers/footers."""
    gray = ImageOps.autocontrast(pil_img.convert("L"))
    w, h = gray.size
    threshold = _compute_otsu_threshold(gray)

    mask = gray.point(lambda p: 255 if p < threshold else 0)
    bbox = mask.getbbox()
    if bbox:
        bx0, by0, bx1, by1 = bbox
        bw = bx1 - bx0
        bh = by1 - by0
        if bw >= 32 and bh >= 32:
            if abs(bw - bh) <= max(bw, bh) * 0.25:
                side = max(bw, bh)
                cx = (bx0 + bx1) // 2
                cy = (by0 + by1) // 2
                x0 = max(0, cx - side // 2)
                y0 = max(0, cy - side // 2)
                x1 = min(w, x0 + side)
                y1 = min(h, y0 + side)
                return pil_img.crop((x0, y0, x1, y1))

    min_dim = min(w, h)
    left = (w - min_dim) // 2
    top = (h - min_dim) // 2
    return pil_img.crop((left, top, left + min_dim, top + min_dim))


def _render_matrix_to_oled_image(payload_text, canvas_size=DEFAULT_OLED_QR_SIZE):
    """Render a QR payload with exact integer module scaling onto a crisp canvas."""
    text = payload_text.strip()
    ecc = qrcode.constants.ERROR_CORRECT_L if len(text) > 75 else qrcode.constants.ERROR_CORRECT_M
    qr = qrcode.QRCode(
        version=None,
        error_correction=ecc,
        box_size=1,
        border=0,
    )
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)

    usable = max(16, canvas_size - 4)
    if n <= usable:
        scale = max(1, usable // n)
        qr_px = n * scale
        offset = (canvas_size - qr_px) // 2
        canvas = Image.new("L", (canvas_size, canvas_size), 255)
        pixels = canvas.load()
        for r in range(n):
            row = matrix[r]
            y_base = offset + r * scale
            for c in range(n):
                if row[c]:
                    x_base = offset + c * scale
                    for dy in range(scale):
                        for dx in range(scale):
                            pixels[x_base + dx, y_base + dy] = 0
        return canvas

    if n <= canvas_size - 2:
        offset = (canvas_size - n) // 2
        canvas = Image.new("L", (canvas_size, canvas_size), 255)
        pixels = canvas.load()
        for r in range(n):
            for c in range(n):
                if matrix[r][c]:
                    pixels[offset + c, offset + r] = 0
        return canvas

    raw_img = qr.make_image(fill_color="#000000", back_color="#ffffff").convert("L")
    return raw_img.resize((canvas_size, canvas_size), Image.Resampling.NEAREST)


def pack_image_to_bitmap_bytes(img, width=DEFAULT_OLED_QR_SIZE, height=DEFAULT_OLED_QR_SIZE, invert=True):
    """Convert a PIL Image into an Adafruit_GFX compatible 1-bit monochrome byte array."""
    gray = img.convert("L")
    if gray.size != (width, height):
        gray = gray.resize((width, height), Image.Resampling.NEAREST)

    threshold = _compute_otsu_threshold(gray)
    mono = gray.point(lambda p: 255 if p > threshold else 0)

    width_bytes = (width + 7) // 8
    byte_array = bytearray(width_bytes * height)

    for y in range(height):
        for x in range(width):
            pixel = mono.getpixel((x, y))
            is_lit = (pixel == 0) if not invert else (pixel != 0)
            if is_lit:
                byte_index = y * width_bytes + (x // 8)
                bit_index = 7 - (x % 8)
                byte_array[byte_index] |= (1 << bit_index)

    return byte_array


def _build_oled_preview_data_uri(bitmap_bytes, width, height):
    """
    Reconstruct the exact 1-bit OLED pixels into an in-memory PNG data URI
    (zero local disk writes, serverless-safe).
    """
    img = Image.new("RGB", (width, height), (0, 0, 0))
    pixels = img.load()
    width_bytes = (width + 7) // 8

    for y in range(height):
        for x in range(width):
            byte_index = y * width_bytes + (x // 8)
            bit_index = 7 - (x % 8)
            is_lit = bool(bitmap_bytes[byte_index] & (1 << bit_index))
            pixels[x, y] = (248, 250, 252) if is_lit else (8, 12, 20)

    upscaled = img.resize((width * 4, height * 4), Image.Resampling.NEAREST)
    buffered = io.BytesIO()
    upscaled.save(buffered, format="PNG")
    encoded = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def _build_and_save_record(bitmap_bytes, width, height, title, amount, subtitle, invert, uploaded_by, qr_text=""):
    """Build normalized display record, generate in-memory preview, and persist to Firestore."""
    bitmap_hex = bitmap_bytes.hex().upper()
    preview_url = _build_oled_preview_data_uri(bitmap_bytes, width, height)

    now_dt = db.now_pht()
    qr_id = f"QR_{int(now_dt.timestamp())}"
    now_str = now_dt.strftime("%Y-%m-%d %I:%M:%S %p")

    clean_title = sanitize_oled_text(title, max_len=20, fallback="")
    clean_amount = str(amount).strip() if amount else ""
    clean_amount_ascii = format_amount_ascii(clean_amount) if clean_amount else ""
    clean_sub = sanitize_oled_text(subtitle, max_len=20, fallback="")

    global active_esp32_qr
    active_esp32_qr = {
        "has_qr": True,
        "qr_id": qr_id,
        "qr_text": (qr_text or "")[:350],
        "title": clean_title,
        "amount": clean_amount,
        "amount_display": clean_amount_ascii,
        "subtitle": clean_sub,
        "width": width,
        "height": height,
        "invert": bool(invert),
        "bitmap_hex": bitmap_hex,
        "preview_url": preview_url,
        "timestamp": now_str,
        "uploaded_by": uploaded_by,
        "displayed_on_hardware": False,
        "hardware_ip": "",
        "hardware_ack_at": None,
    }

    try:
        saved = db.save_active_display_qr(active_esp32_qr, uploaded_by=uploaded_by)
        if isinstance(saved, dict):
            active_esp32_qr.update(saved)
    except Exception as e:
        print(f"[QR SERVICE] Firestore save warning: {e}")

    return active_esp32_qr


def generate_qr_from_payload(
    payload_text,
    title="",
    amount="",
    subtitle="",
    invert=True,
    uploaded_by="Admin",
    canvas_size=DEFAULT_OLED_QR_SIZE,
):
    """Generates an integer-scaled QR code from text/link, formats for OLED, and saves to Firestore."""
    clean_payload = (payload_text or "").strip()
    img = _render_matrix_to_oled_image(clean_payload, canvas_size=canvas_size)
    bitmap_bytes = pack_image_to_bitmap_bytes(img, width=canvas_size, height=canvas_size, invert=invert)
    return _build_and_save_record(
        bitmap_bytes=bitmap_bytes,
        width=canvas_size,
        height=canvas_size,
        title=title,
        amount=amount,
        subtitle=subtitle,
        invert=invert,
        uploaded_by=uploaded_by,
        qr_text=clean_payload,
    )


def process_uploaded_qr_image(
    file_storage,
    title="",
    amount="",
    subtitle="",
    invert=True,
    uploaded_by="Admin",
    decoded_qr_text=None,
    canvas_size=DEFAULT_OLED_QR_SIZE,
):
    """Processes an uploaded QR image file in memory without local disk writes."""
    img = Image.open(file_storage).convert("RGB")

    extracted_text = (decoded_qr_text or "").strip() or _decode_qr_with_cv2(img)
    if extracted_text:
        return generate_qr_from_payload(
            extracted_text,
            title=title,
            amount=amount,
            subtitle=subtitle,
            invert=invert,
            uploaded_by=uploaded_by,
            canvas_size=canvas_size,
        )

    img_square = _smart_crop_qr_region(img)
    img_resized = img_square.convert("L").resize((canvas_size, canvas_size), Image.Resampling.BILINEAR)
    bitmap_bytes = pack_image_to_bitmap_bytes(img_resized, width=canvas_size, height=canvas_size, invert=invert)
    return _build_and_save_record(
        bitmap_bytes=bitmap_bytes,
        width=canvas_size,
        height=canvas_size,
        title=title,
        amount=amount,
        subtitle=subtitle,
        invert=invert,
        uploaded_by=uploaded_by,
        qr_text="",
    )


def get_active_display_qr():
    """Reads directly from Firestore for stateless multi-instance consistency."""
    global active_esp32_qr
    try:
        db_record = db.get_active_display_qr()
        if db_record and isinstance(db_record, dict):
            if "amount_display" not in db_record:
                db_record["amount_display"] = format_amount_ascii(db_record.get("amount", ""))
            active_esp32_qr = db_record
            return db_record
    except Exception as e:
        print(f"[QR SERVICE] Firestore fetch warning: {e}")
    return active_esp32_qr


def clear_active_display_qr():
    """Clears active display QR in Firestore and local fallback cache."""
    global active_esp32_qr
    try:
        db.clear_active_display_qr()
    except Exception as e:
        print(f"[QR SERVICE] Firestore clear warning: {e}")

    active_esp32_qr = {
        "has_qr": False,
        "qr_id": "",
        "qr_text": "",
        "title": "SYSTEM READY",
        "amount": "",
        "amount_display": "",
        "subtitle": "Standby Mode",
        "width": DEFAULT_OLED_QR_SIZE,
        "height": DEFAULT_OLED_QR_SIZE,
        "invert": True,
        "bitmap_hex": "",
        "preview_url": "",
        "timestamp": db.now_pht().strftime("%Y-%m-%d %I:%M:%S %p"),
        "displayed_on_hardware": False,
        "hardware_ip": "",
        "hardware_ack_at": None,
    }
    return active_esp32_qr


def acknowledge_hardware_display(qr_id, ip_address="127.0.0.1", device_name="ESP32"):
    """ESP32 calls this to confirm the QR code was painted on the physical screen."""
    global active_esp32_qr
    now_str = db.now_pht().strftime("%b %d, %Y • %I:%M:%S %p")
    if not qr_id or active_esp32_qr.get("qr_id") == qr_id:
        active_esp32_qr["displayed_on_hardware"] = True
        active_esp32_qr["hardware_ip"] = ip_address
        active_esp32_qr["hardware_device"] = device_name
        active_esp32_qr["hardware_ack_at"] = now_str
    try:
        db.ack_hardware_qr_displayed(qr_id, ip_address, device_name=device_name)
    except Exception as e:
        print(f"[QR SERVICE] Firestore ack warning: {e}")
    return True
