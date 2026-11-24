import re
import os
import time
import math
from collections import Counter
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image

# =====================================================================
# 1. SECURITY CONSTANTS & PATTERNS
# =====================================================================
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024  # 16 MB

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')
USERNAME_REGEX = re.compile(r'^[a-zA-Z0-9_\- ]{3,50}$')
CODE_REGEX = re.compile(r'^[a-zA-Z0-9_\-]{2,50}$')

# =====================================================================
# 2. PASSWORD HASHING & BACKWARDS-COMPATIBLE VERIFICATION
# =====================================================================
def hash_password(plain_password):
    """Securely hash a password using standard PBKDF2/scrypt"""
    return generate_password_hash(str(plain_password).strip())

def verify_password(stored_password, provided_password):
    """
    Flexible verification supporting both modern hashed passwords and legacy plaintext.
    Returns: (is_valid: bool, needs_upgrade: bool)
    If needs_upgrade is True, caller should re-hash and save the password.
    """
    if not stored_password or not provided_password:
        return False, False

    stored_str = str(stored_password).strip()
    provided_str = str(provided_password).strip()

    # Check if stored_password is a werkzeug hash (starts with method prefix like 'scrypt:' or 'pbkdf2:')
    if stored_str.startswith(('scrypt:', 'pbkdf2:')):
        try:
            is_valid = check_password_hash(stored_str, provided_str)
            return is_valid, False
        except Exception:
            return False, False

    # Legacy plaintext check
    if stored_str == provided_str:
        return True, True  # Matched plaintext, needs upgrade to secure hash!

    return False, False

def validate_password_strength(password, min_length=6, max_length=128):
    """Validate password length and character limits"""
    if not password:
        return False, "Password cannot be empty."
    p_str = str(password)
    if len(p_str) < min_length:
        return False, f"Password must be at least {min_length} characters long."
    if len(p_str) > max_length:
        return False, f"Password cannot exceed {max_length} characters."
    return True, None

# =====================================================================
# 3. USER INPUT SANITIZERS & VALIDATORS
# =====================================================================
def sanitize_text(text, max_length=255, allow_newlines=False):
    """Strip dangerous characters, HTML tags, and null bytes to prevent XSS"""
    if text is None:
        return ""
    val = str(text).replace('\x00', '')
    if not allow_newlines:
        val = val.replace('\r', '').replace('\n', ' ')
    # Strip HTML tags
    val = re.sub(r'<[^>]*>', '', val)
    val = val.strip()
    if max_length and len(val) > max_length:
        val = val[:max_length]
    return val

def validate_username(username):
    """Validates username: 3-50 characters, alphanumeric, underscores, hyphens, spaces"""
    if not username:
        return False, "", "Username is required."
    clean = sanitize_text(username, max_length=50)
    if len(clean) < 3:
        return False, clean, "Username must be at least 3 characters long."
    if not USERNAME_REGEX.match(clean):
        return False, clean, "Username can only contain letters, numbers, spaces, hyphens, and underscores."
    return True, clean, None

def validate_email(email, allow_empty=False):
    """Validates email format and blocks CRLF injection"""
    if not email or not str(email).strip():
        if allow_empty:
            return True, "", None
        return False, "", "Email address is required."

    clean = str(email).strip().lower()
    # Check for CRLF injection
    if '\r' in clean or '\n' in clean:
        return False, "", "Invalid email format (disallowed linebreaks)."

    if len(clean) > 150:
        return False, "", "Email address is too long (max 150 characters)."

    if not EMAIL_REGEX.match(clean):
        return False, "", "Please provide a valid email address (e.g. user@gmail.com)."

    return True, clean, None

def validate_contact_no(contact_no, allow_empty=True):
    """Validates mobile/phone number (supports PH 11-digit format and international)"""
    if not contact_no or not str(contact_no).strip():
        if allow_empty:
            return True, "", None
        return False, "", "Contact number is required."

    raw = str(contact_no).strip()
    # Clean digits only
    digits = re.sub(r'[^0-9]', '', raw)
    if len(digits) < 7 or len(digits) > 15:
        return False, raw, "Contact number must be between 7 and 15 digits."
    return True, digits, None

PERSON_NAME_ALLOWED_CHARS = re.compile(r"^[A-Za-zÀ-ÿ\u00f1\u00d1\s\.\-']+$")

def validate_person_name(name, field_name="Full Name", max_length=40, min_length=2):
    """
    Strict validation for client, deceased, and customer person names.
    Stress-test hardened to pass strict academic and mobile testing:
    - Strictly limits length to max_length (default 40 characters)
    - Minimum length of 2 characters
    - Strictly prevents repeating characters in a row (e.g. 'yyyyyyyyyyyy', 'aaaa')
    - Prevents single-character dominant spam (e.g. 'a a a a a')
    - Strictly forbids numbers (0-9) and special symbols
    - Requires at least 2 alphabetic letters and valid vowel structure (prevents keyboard smashing like 'bcdfghjkl')
    - Prevents leading/trailing or consecutive punctuation marks (e.g. '-John', 'John..')
    """
    if name is None:
        return False, "", f"{field_name} is required."

    # Remove null bytes, HTML tags, normalize whitespace
    clean = re.sub(r'\s+', ' ', sanitize_text(name, max_length=150)).strip()

    if not clean:
        return False, "", f"{field_name} is required."

    if len(clean) < min_length:
        return False, clean, f"{field_name} must be at least {min_length} characters long."

    if len(clean) > max_length:
        return False, clean, f"{field_name} cannot exceed {max_length} characters (currently {len(clean)} characters)."

    # 1. No numbers allowed in human names
    if re.search(r'\d', clean):
        return False, clean, f"{field_name} cannot contain numbers. Please use letters only."

    # 2. Check for disallowed special characters/symbols
    if not PERSON_NAME_ALLOWED_CHARS.match(clean) or '_' in clean:
        return False, clean, f"{field_name} contains invalid symbols. Only letters, spaces, hyphens (-), apostrophes ('), and periods (.) are allowed."

    # 3. Cannot start or end with punctuation marks or spaces
    if clean[0] in ".-' " or clean[-1] in ".-' ":
        return False, clean, f"{field_name} cannot start or end with a hyphen, dot, apostrophe, or space."

    # 4. No consecutive punctuation marks (e.g., '..', '--', '.-', "''")
    if re.search(r'[\.\-\']{2,}', clean):
        return False, clean, f"{field_name} cannot contain consecutive punctuation marks (e.g., '..', '--')."

    # 5. Strictly prevent repeating characters in a row (4 or more identical consecutive characters)
    # Catches 'yyyyyyyyyyyyyyyyyyyyyyy', 'aaaa', '-----', etc.
    repeat_match = re.search(r'(.)\1{3,}', clean, re.IGNORECASE)
    if repeat_match:
        rep_char = repeat_match.group(0)
        return False, clean, f"{field_name} cannot contain repeated characters in a row (found '{rep_char}'). Maximum 4 in a row allowed (no 'yyyyy'). Please enter a realistic name."

    # 6. Extract alphabetic letters (including Filipino ñ/Ñ and accented vowels)
    letters = re.findall(r'[A-Za-zÀ-ÿ\u00f1\u00d1]', clean.lower())
    if len(letters) < min_length:
        return False, clean, f"{field_name} must contain at least {min_length} alphabetic letters."

    # 7. Prevent single-character dominant spam across the whole name (e.g. "a a a a a", "x y x x x x")
    if len(letters) >= 4:
        counts = Counter(letters)
        most_common_char, count = counts.most_common(1)[0]
        if (count / len(letters)) > 0.60:
            return False, clean, f"{field_name} has too many repeated letters ('{most_common_char}'). Please enter a realistic name."

    # 8. Check for repeating word loops (e.g., "Juan Juan Juan")
    words = clean.lower().split()
    if len(words) >= 3 and len(set(words)) == 1:
        return False, clean, f"{field_name} cannot be the same word repeated multiple times."

    # 9. Vowel & pronounceability check for names >= 3 letters (blocks keyboard smashing like "bcdfghjkl")
    if len(letters) >= 3:
        vowels = re.findall(r'[aeiouyÀ-ÿ\u00f1\u00d1]', clean.lower())
        if not vowels:
            return False, clean, f"{field_name} must contain at least one vowel (a, e, i, o, u, y)."

    # 10. Reject excessive unnatural consonant clusters (6+ consonants in a row)
    if re.search(r'[bcdfghjklmnpqrstvwxz]{6,}', clean, re.IGNORECASE):
        return False, clean, f"{field_name} contains an unrealistic sequence of consonants."

    # 11. Block common QA/test dummy placeholders (e.g. 'test', 'dummy', 'asdf', 'n/a', 'sample', 'unknown')
    lower_clean = clean.lower()
    PLACEHOLDER_NAMES = {
        'test', 'testing', 'tester', 'dummy', 'sample', 'example',
        'asdf', 'qwerty', 'foo', 'bar', 'baz', 'null', 'none',
        'n/a', 'na', 'unknown', 'anonymous', 'tba', 'tbd', 'placeholder'
    }
    if lower_clean in PLACEHOLDER_NAMES:
        return False, clean, f"{field_name} cannot be a test placeholder (e.g., '{clean}'). Please enter an actual name."

    # Block any individual word being a test word (e.g. 'Test Cruz', 'Dummy Deceased')
    for w in words:
        if w in {'test', 'testing', 'dummy', 'asdf', 'qwerty', 'null', 'none', 'n/a'}:
            return False, clean, f"{field_name} contains a test placeholder word ('{w}'). Please enter an actual name."

    # 12. Prevent strings of disjoint single letters (e.g., 'a b c d e')
    single_letters = [w for w in words if len(w) == 1 and not w.endswith('.')]
    if len(single_letters) >= 3 or (len(words) >= 3 and len(single_letters) == len(words)):
        return False, clean, f"{field_name} appears to be random single letters. Please enter a valid name."

    return True, clean, None

def validate_ph_mobile(contact_no, allow_empty=False):
    """
    Strict validation for Philippine mobile numbers:
    - Normalizes common phone formatting (+63, 63, spaces, hyphens) to 09XXXXXXXXX
    - Must be exactly 11 digits starting with '09'
    - Rejects all-identical dummy numbers (e.g. 09000000000, 09111111111, 09999999999)
    - Rejects 5 or more repeating digits in a row
    """
    if not contact_no or not str(contact_no).strip():
        if allow_empty:
            return True, "", None
        return False, "", "Mobile number is required (11 digits starting with 09)."

    raw = str(contact_no).strip()

    # Normalize international / formatting prefixes (+63 or 63)
    digits = re.sub(r'[^0-9]', '', raw)
    if digits.startswith('63') and len(digits) == 12:
        digits = '0' + digits[2:]
    elif digits.startswith('9') and len(digits) == 10:
        digits = '0' + digits

    if not re.match(r'^09\d{9}$', digits):
        return False, digits, "Please enter a valid 11-digit Philippine mobile number starting with 09 (e.g., 09171234567)."

    # Reject all identical digits in the subscriber portion (e.g. 09000000000, 09999999999)
    if len(set(digits[2:])) <= 1:
        return False, digits, "Mobile number cannot consist of all identical dummy digits (e.g., 09000000000)."

    # Reject 5 or more repeating digits in a row (e.g. 09178888881)
    if re.search(r'(\d)\1{4,}', digits):
        return False, digits, "Mobile number cannot contain 5 or more repeating digits in a row."

    return True, digits, None

def validate_role(role):
    """Validate system account role (Strictly Admin or Staff)"""
    clean = str(role or '').strip().capitalize()
    if clean in ('Admin', 'Staff'):
        return clean
    return 'Staff'

# =====================================================================
# 4. POS, BILLING & NUMERIC VALIDATORS
# =====================================================================
def validate_positive_number(val, field_name="Amount", allow_zero=True, max_val=10000000.0):
    """
    Safely parse and validate positive currency / numbers.
    Strictly defends against:
    - Negative amounts (num < 0)
    - Negative zero strings ('-0', '-0.0', '-0.00', '- 0')
    - IEEE 754 negative zero float (-0.0)
    - NaN, Infinity, -Infinity
    - Overflow (> max_val)
    - Non-numeric or malformed text
    """
    if val is None or str(val).strip() == '':
        if allow_zero:
            return True, 0.0, None
        return False, 0.0, f"{field_name} is required."

    val_str = str(val).strip().lower()
    if val_str in ('nan', 'inf', '-inf', 'infinity', '-infinity', 'null', 'undefined'):
        return False, 0.0, f"{field_name} must be a valid numeric amount."

    # Remove currency symbols, commas, and whitespace
    clean_str = val_str.replace(',', '').replace('₱', '').replace('$', '').strip()

    # Explicit check: strictly block negative signs anywhere in the number
    # This catches '-0', '-0.0', '-0.00', '--5', '- 0', etc.
    if '-' in clean_str:
        return False, 0.0, f"{field_name} cannot be negative or contain a negative sign (e.g., '-0')."

    try:
        num = float(clean_str)

        if math.isnan(num) or math.isinf(num):
            return False, 0.0, f"{field_name} must be a valid numeric amount."

        # Detect IEEE 754 floating-point negative zero (-0.0)
        if num < 0 or (num == 0.0 and math.copysign(1.0, num) < 0):
            return False, 0.0, f"{field_name} cannot be negative or negative zero (e.g., '-0')."

        if not allow_zero and num <= 0:
            return False, 0.0, f"{field_name} must be greater than ₱0."

        if num > max_val:
            return False, 0.0, f"{field_name} exceeds the maximum limit (₱{max_val:,.2f})."

        return True, round(abs(num), 2), None
    except (ValueError, TypeError):
        return False, 0.0, f"{field_name} must be a valid numeric amount."

def validate_stock_quantity(val, max_stock=50000):
    """Validates inventory stock count (integer >= 0, blocks -0 and negative values)"""
    if val is None or str(val).strip() == '':
        return False, 0, "Stock quantity is required."

    val_str = str(val).strip()
    if '-' in val_str:
        return False, 0, "Stock cannot be negative or contain a negative sign (e.g., '-0')."

    try:
        clean_str = val_str.replace(',', '').strip()
        num = float(clean_str)
        if math.isnan(num) or math.isinf(num) or num < 0 or (num == 0.0 and math.copysign(1.0, num) < 0):
            return False, 0, "Stock cannot be negative."
        int_num = int(num)
        if int_num > max_stock:
            return False, 0, f"Stock quantity exceeds realistic maximum ({max_stock:,})."
        return True, int_num, None
    except (ValueError, TypeError):
        return False, 0, "Stock must be a valid integer."

def validate_code(code, field_name="Code"):
    """Validates alphanumeric codes like CSK-OAK-01 or Invoice numbers"""
    if not code:
        return False, "", f"{field_name} is required."
    clean = sanitize_text(code, max_length=50).upper()
    if not CODE_REGEX.match(clean):
        return False, clean, f"{field_name} can only contain letters, numbers, hyphens, and underscores."
    return True, clean, None

# =====================================================================
# 5. FILE UPLOAD SECURITY (MAGIC BYTES & EXTENSION CHECK)
# =====================================================================
def validate_image_file(file_storage):
    """
    Thoroughly inspects uploaded image file:
    1. Validates extension
    2. Uses Pillow to verify it is an actual valid image
    3. Blocks malicious files masquerading as images
    """
    if not file_storage or not file_storage.filename:
        return False, None, "No file uploaded."

    filename = secure_filename(file_storage.filename)
    if not filename or '.' not in filename:
        return False, None, "Invalid file name."

    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return False, None, f"Unsupported file type (.{ext}). Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}."

    # Inspect image header & content with Pillow
    try:
        file_storage.seek(0)
        img = Image.open(file_storage)
        img.verify()  # Verifies file integrity & header
        file_storage.seek(0)  # Reset pointer for saving
        
        # Verify format matches allowed types
        format_lower = str(img.format).lower() if img.format else ''
        valid_formats = {'png', 'jpeg', 'jpg', 'gif', 'webp'}
        if format_lower not in valid_formats and ext not in ('jpg', 'jpeg'):
            return False, None, f"File content does not match image extension (Format: {img.format})."

        return True, filename, None
    except Exception as e:
        file_storage.seek(0)
        return False, None, f"Uploaded file is corrupted or not a valid image: {e}"

# =====================================================================
# 6. IN-MEMORY SLIDING RATE LIMITER (BRUTE FORCE & FLOODING DEFENSE)
# =====================================================================
class SimpleRateLimiter:
    """Lightweight in-memory rate limiter for login and sensitive actions"""
    def __init__(self):
        self.attempts = {}  # {key: [timestamp, timestamp, ...]}

    def is_limited(self, key, max_attempts=5, window_seconds=300):
        """Returns (is_limited: bool, retry_after: int)"""
        now = time.time()
        timestamps = self.attempts.get(key, [])
        # Prune expired
        timestamps = [t for t in timestamps if now - t < window_seconds]
        self.attempts[key] = timestamps

        if len(timestamps) >= max_attempts:
            oldest = timestamps[0]
            retry_after = int(window_seconds - (now - oldest)) + 1
            return True, max(1, retry_after)

        return False, 0

    def record_attempt(self, key):
        """Records an attempt for the given key"""
        now = time.time()
        if key not in self.attempts:
            self.attempts[key] = []
        self.attempts[key].append(now)

    def clear(self, key):
        """Clears attempts for a key upon successful action"""
        if key in self.attempts:
            del self.attempts[key]

# Global instance for app
rate_limiter = SimpleRateLimiter()

