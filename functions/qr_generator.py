import io
import base64
import qrcode


def sanitize_qr_code_slug(casket_code):
    """Normalize package code into a safe uppercase identifier."""
    if not casket_code:
        return "PKG"
    code_safe = "".join(c for c in str(casket_code) if c.isalnum() or c in ("-", "_")).upper()
    return code_safe or "PKG"


def generate_package_qr_bytes(casket_code):
    """
    Generate a crisp QR PNG purely in memory (no local filesystem writes,
    safe for read-only Cloud Functions / Cloud Run containers).
    """
    if not casket_code:
        return None

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(str(casket_code).strip())
    qr.make(fit=True)
    img = qr.make_image(fill_color="#000000", back_color="#ffffff")

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return buffered.getvalue()


def generate_package_qr(casket_code):
    """
    Generate a unique QR code for a funeral package entirely in memory.
    Encodes the package's unique casket_code (e.g. 'CSK-OAK-01').
    Returns a tuple of (dynamic_png_url, base64_data_uri).
    """
    if not casket_code:
        return None, None

    code_safe = sanitize_qr_code_slug(casket_code)
    png_bytes = generate_package_qr_bytes(casket_code)
    if not png_bytes:
        return None, None

    img_b64 = base64.b64encode(png_bytes).decode("utf-8")
    data_uri = f"data:image/png;base64,{img_b64}"
    relative_url = f"/api/qr/{code_safe}.png"
    return relative_url, data_uri
