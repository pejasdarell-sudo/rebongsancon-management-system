import os
import io
import base64
import qrcode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QR_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'qrcodes')
os.makedirs(QR_FOLDER, exist_ok=True)

def generate_package_qr(casket_code):
    """
    Generate a unique QR code for a package.
    Encodes the package's unique casket_code (e.g. 'CSK-OAK-01').
    Saves the PNG into static/uploads/qrcodes/<casket_code>.png
    Returns a tuple of (relative_url, base64_data_uri).
    """
    if not casket_code:
        return None, None

    code_safe = "".join(c for c in casket_code if c.isalnum() or c in ('-', '_')).upper()
    if not code_safe:
        code_safe = "PKG"
    filename = f"{code_safe}.png"
    filepath = os.path.join(QR_FOLDER, filename)

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    # The QR code data is the unique package code so scanners (ESP32 and Mobile App) can query Firestore
    qr.add_data(casket_code.strip())
    qr.make(fit=True)
    img = qr.make_image(fill_color="#000000", back_color="#ffffff")
    img.save(filepath)

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
    data_uri = f"data:image/png;base64,{img_b64}"

    relative_url = f"/static/uploads/qrcodes/{filename}"
    return relative_url, data_uri

