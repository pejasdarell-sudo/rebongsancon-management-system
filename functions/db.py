import os
import json
import uuid
import mimetypes
from urllib.parse import quote
from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter

import validators
from qr_generator import generate_package_qr, sanitize_qr_code_slug

# =====================================================================
# 1. ENVIRONMENT, TIMEZONE & LAZY FIREBASE ADMIN SDK INITIALIZATION
# =====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_CLOUD = bool(os.environ.get("K_SERVICE") or os.environ.get("FUNCTION_TARGET"))

try:
    PHT = ZoneInfo("Asia/Manila")
except Exception:
    PHT = timezone(timedelta(hours=8))


def now_pht():
    """Return current datetime in Philippine Time (Asia/Manila, UTC+8)."""
    return datetime.now(PHT)


def to_pht(dt_val):
    """Normalize a datetime / Firestore timestamp to Asia/Manila timezone."""
    if not isinstance(dt_val, datetime):
        return dt_val
    if dt_val.tzinfo is None:
        return dt_val.replace(tzinfo=PHT)
    return dt_val.astimezone(PHT)


def _find_local_service_account():
    """Locate serviceAccountKey.json for local development only (never required in cloud)."""
    candidates = [
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
        os.path.join(BASE_DIR, "serviceAccountKey.json"),
        os.path.abspath(os.path.join(BASE_DIR, "..", "Rebong Sancon Funeral Service", "serviceAccountKey.json")),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _detect_project_id(sa_path=None):
    """Determine Firebase project ID from environment or local service account metadata."""
    for env_key in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCP_PROJECT", "FIREBASE_PROJECT_ID"):
        val = (os.environ.get(env_key) or "").strip()
        if val:
            return val
    fb_cfg = os.environ.get("FIREBASE_CONFIG")
    if fb_cfg:
        try:
            parsed = json.loads(fb_cfg)
            if parsed.get("projectId"):
                return str(parsed["projectId"]).strip()
        except Exception:
            pass
    if sa_path and os.path.isfile(sa_path):
        try:
            with open(sa_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("project_id"):
                    return str(data["project_id"]).strip()
        except Exception:
            pass
    return "rebongsancondb"


def _detect_storage_bucket(project_id):
    """Resolve Firebase Storage bucket name (supports both new .firebasestorage.app and .appspot.com)."""
    env_bucket = (os.environ.get("FIREBASE_STORAGE_BUCKET") or "").strip()
    if env_bucket:
        return env_bucket
    fb_cfg = os.environ.get("FIREBASE_CONFIG")
    if fb_cfg:
        try:
            parsed = json.loads(fb_cfg)
            if parsed.get("storageBucket"):
                return str(parsed["storageBucket"]).strip()
        except Exception:
            pass
    return f"{project_id}.firebasestorage.app"


_firestore_client = None


def _ensure_firebase_app():
    """
    Lazily initialize Firebase Admin SDK:
    - In Cloud Functions / Cloud Run: uses Application Default Credentials (IAM).
    - Locally: uses serviceAccountKey.json if present, else ADC.
    Deferring initialization avoids credential errors during `firebase deploy` discovery.
    """
    if firebase_admin._apps:
        return firebase_admin.get_app()

    sa_path = None if IS_CLOUD else _find_local_service_account()
    project_id = _detect_project_id(sa_path)
    bucket_name = _detect_storage_bucket(project_id)
    app_options = {"projectId": project_id, "storageBucket": bucket_name}

    if sa_path:
        cred = credentials.Certificate(sa_path)
        return firebase_admin.initialize_app(cred, options=app_options)
    return firebase_admin.initialize_app(options=app_options)


def get_firestore_client():
    """Return cached Firestore client, lazily initializing Firebase Admin SDK on first use."""
    global _firestore_client
    if _firestore_client is None:
        _ensure_firebase_app()
        _firestore_client = firestore.client()
    return _firestore_client


def get_storage_bucket(bucket_name=None):
    """Return Firebase Storage bucket object."""
    _ensure_firebase_app()
    if bucket_name:
        return storage.bucket(bucket_name)
    return storage.bucket()


class _LazyFirestoreProxy:
    """Transparent proxy so `db.collection(...)` works lazily with zero import-time network calls."""

    def __getattr__(self, name):
        return getattr(get_firestore_client(), name)

    def __bool__(self):
        return True


db = _LazyFirestoreProxy()


def upload_image_to_storage(file_storage, folder="caskets", unique_name=None):
    """
    Upload a verified Flask FileStorage image to Firebase Storage using a permanent
    Firebase Storage download token URL (works even with Uniform Bucket-Level Access enabled).
    Returns the public HTTPS download URL or None on failure.
    """
    if not file_storage or not unique_name:
        return None

    app_inst = _ensure_firebase_app()
    project_id = app_inst.project_id or _detect_project_id()
    configured_bucket = app_inst.options.get("storageBucket") or _detect_storage_bucket(project_id)

    candidate_buckets = [configured_bucket]
    for fallback_b in (f"{project_id}.firebasestorage.app", f"{project_id}.appspot.com"):
        if fallback_b not in candidate_buckets:
            candidate_buckets.append(fallback_b)

    blob_path = f"uploads/{folder.strip('/')}/{unique_name}"
    content_type = (
        getattr(file_storage, "mimetype", None)
        or mimetypes.guess_type(unique_name)[0]
        or "application/octet-stream"
    )
    token = str(uuid.uuid4())

    file_storage.seek(0)
    raw_bytes = file_storage.read()
    file_storage.seek(0)

    last_err = None
    for b_name in candidate_buckets:
        try:
            bucket = storage.bucket(b_name)
            blob = bucket.blob(blob_path)
            blob.metadata = {"firebaseStorageDownloadTokens": token}
            blob.cache_control = "public, max-age=31536000"
            blob.upload_from_string(raw_bytes, content_type=content_type)
            encoded_path = quote(blob_path, safe="")
            return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{encoded_path}?alt=media&token={token}"
        except Exception as e:
            last_err = e
            print(f"[STORAGE UPLOAD WARNING] Bucket '{b_name}' failed: {e}")

    print(f"[STORAGE UPLOAD ERROR] All candidate buckets failed: {last_err}")
    return None


# =====================================================================
# HELPER UTILITIES
# =====================================================================
def _format_time_12hr(time_val):
    """Format any time string or object into 12-hour format with AM/PM."""
    if not time_val:
        return "--"
    t_str = str(time_val).strip()
    if "AM" in t_str.upper() or "PM" in t_str.upper():
        return t_str
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S", "%I:%M"):
        try:
            dt = datetime.strptime(t_str, fmt)
            return dt.strftime("%I:%M:%S %p")
        except ValueError:
            pass
    return t_str


def _format_datetime(dt_val):
    """Format any datetime / Firestore timestamp to readable PHT string."""
    if not dt_val:
        return "--"
    if isinstance(dt_val, datetime):
        return to_pht(dt_val).strftime("%b %d, %Y • %I:%M %p")
    if hasattr(dt_val, "strftime"):
        try:
            return dt_val.strftime("%b %d, %Y • %I:%M %p")
        except Exception:
            pass
    return str(dt_val)


def _safe_float(val, default=0.0):
    """Safely convert any currency string or numeric value to float."""
    if val is None or val == "":
        return default
    try:
        if isinstance(val, (int, float)):
            return float(val)
        val_str = str(val).replace("₱", "").replace(",", "").strip()
        return float(val_str)
    except Exception:
        return default


def _sanitize_dict(d):
    """Convert custom objects / dates into JSON-serializable dict."""
    if not isinstance(d, dict):
        return d
    res = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            res[k] = to_pht(v).isoformat()
        elif isinstance(v, date):
            res[k] = v.isoformat()
        elif isinstance(v, dict):
            res[k] = _sanitize_dict(v)
        elif isinstance(v, list):
            res[k] = [_sanitize_dict(i) if isinstance(i, dict) else i for i in v]
        else:
            res[k] = v
    return res


def _normalize_casket_doc(doc):
    """Normalize a Firestore casket document without touching local disk."""
    item = doc.to_dict() or {}
    item["id"] = doc.id
    code = str(item.get("casket_code") or doc.id).strip()
    item["casket_code"] = code
    item["base_price"] = _safe_float(item.get("base_price", 0))
    item["stock"] = int(item.get("stock", 0) or 0)
    if not item.get("qr_code"):
        item["qr_code"] = code

    code_slug = sanitize_qr_code_slug(code)
    dynamic_qr_url = f"/api/qr/{code_slug}.png"
    stored_qr_url = str(item.get("qr_image_url") or "")
    if not stored_qr_url or stored_qr_url.startswith("/static/uploads/qrcodes/"):
        item["qr_image_url"] = dynamic_qr_url

    if not item.get("qr_base64"):
        try:
            q_url, q_b64 = generate_package_qr(code)
            if q_url:
                item["qr_image_url"] = q_url
            if q_b64:
                item["qr_base64"] = q_b64
        except Exception as qr_err:
            print(f"[QR GEN WARNING] {code}: {qr_err}")

    if isinstance(item.get("inclusions"), str):
        try:
            item["inclusions"] = json.loads(item["inclusions"])
        except Exception:
            item["inclusions"] = [item["inclusions"]]
    elif not item.get("inclusions"):
        item["inclusions"] = []

    return item


# =====================================================================
# 2. EXPLICIT SEEDING UTILITIES (Call via scripts/seed_firestore.py)
# =====================================================================
def seed_initial_data_if_empty():
    """Populate default admin/staff accounts and showroom caskets if collections are empty."""
    try:
        users_ref = db.collection("users")
        if len(list(users_ref.limit(1).stream())) == 0:
            users_ref.document("admin").set({
                "username": "admin",
                "password": validators.hash_password("admin123"),
                "full_name": "Administrator Staff",
                "email": "admin@rebongsancon.com",
                "contact_no": "09170000000",
                "role": "Admin",
                "must_change_password": False,
                "created_at": now_pht(),
            })
            users_ref.document("staff").set({
                "username": "staff",
                "password": validators.hash_password("staff123"),
                "full_name": "Frontline Staff",
                "email": "staff@rebongsancon.com",
                "contact_no": "09180000000",
                "role": "Staff",
                "must_change_password": True,
                "created_at": now_pht(),
            })
            print("[FIRESTORE] Seeded default users (admin, staff).")

        caskets_ref = db.collection("caskets")
        default_caskets = [
            {
                "casket_code": "CSK-OAK-01",
                "name": "Presidential Solid Oak Funeral Package",
                "category": "Hardwood Casket Package",
                "material": "Solid Red Oak with Velvet Interior",
                "dimensions": '82" L x 28" W x 23" H',
                "base_price": 85000.0,
                "stock": 4,
                "inclusions": [
                    "Professional Embalming & Cosmetic Restoration",
                    "5 Days Air-conditioned Chapel Vigil Setup",
                    "Glass-walled Funeral Hearse to Cemetery",
                    "Memorial Guest Registry Book & 100 Mass Cards",
                    "Burial Permit Assistance & Coordination",
                ],
                "image_url": None,
                "created_at": now_pht(),
            },
            {
                "casket_code": "CSK-MAHOG-02",
                "name": "Executive Mahogany Elegance Package",
                "category": "Hardwood Casket Package",
                "material": "Polished Mahogany Wood with Crepe Lining",
                "dimensions": '80" L x 27" W x 22" H',
                "base_price": 65000.0,
                "stock": 2,
                "inclusions": [
                    "Complete Embalming & Dressing Service",
                    "4 Days Viewing at Chapel or Private Residence",
                    "Dedicated Modern Hearse Escort",
                    "Crucifix & Memorial Vigil Lamp Setup",
                ],
                "image_url": None,
                "created_at": now_pht(),
            },
            {
                "casket_code": "CSK-MET-03",
                "name": "Heritage Stainless Metal Sealer Package",
                "category": "Gasketed Metal Casket",
                "material": "18-Gauge Protective Gasketed Steel",
                "dimensions": '83" L x 28" W x 24" H',
                "base_price": 110000.0,
                "stock": 1,
                "inclusions": [
                    "Hermetically Sealed Protection",
                    "Complete Embalming & Full Dressing",
                    "7 Days Air-conditioned Premier Chapel Vigil",
                    "Cadillac Funeral Hearse Service",
                    "2 Floral Stand Arrangements & Register Stand",
                ],
                "image_url": None,
                "created_at": now_pht(),
            },
            {
                "casket_code": "CSK-PINE-04",
                "name": "Serenity Natural Pine Simple Package",
                "category": "Standard Wood Casket",
                "material": "Natural Pine Wood with Satin Lining",
                "dimensions": '80" L x 26" W x 22" H',
                "base_price": 42000.0,
                "stock": 5,
                "inclusions": [
                    "Sanitization & Standard Embalming",
                    "3 Days Chapel or Home Vigil",
                    "Standard Funeral Hearse Escort",
                ],
                "image_url": None,
                "created_at": now_pht(),
            },
        ]

        for pkg in default_caskets:
            doc_ref = caskets_ref.document(pkg["casket_code"])
            if not doc_ref.get().exists:
                gen_url, gen_b64 = generate_package_qr(pkg["casket_code"])
                pkg["qr_code"] = pkg["casket_code"]
                pkg["qr_image_url"] = gen_url
                pkg["qr_base64"] = gen_b64
                doc_ref.set(pkg)
    except Exception as e:
        print(f"[FIRESTORE SEED ERROR] {e}")


def sync_all_package_qrs():
    """Ensure every package in Firestore has qr_code, qr_image_url (/api/qr/...), and qr_base64."""
    try:
        caskets_ref = db.collection("caskets")
        for doc in caskets_ref.stream():
            data = doc.to_dict() or {}
            code = data.get("casket_code", doc.id)
            qr_url = str(data.get("qr_image_url") or "")
            if (
                not qr_url
                or qr_url.startswith("/static/uploads/qrcodes/")
                or not data.get("qr_code")
                or not data.get("qr_base64")
            ):
                gen_url, gen_b64 = generate_package_qr(code)
                caskets_ref.document(doc.id).update({
                    "qr_code": code,
                    "qr_image_url": gen_url,
                    "qr_base64": gen_b64,
                })
                print(f"[FIRESTORE QR] Synced cloud QR code for package: {code}")
    except Exception as e:
        print(f"[FIRESTORE QR SYNC ERROR] {e}")


# =====================================================================
# 3. USERS & AUTHENTICATION (FIRESTORE)
# =====================================================================
def get_user_by_username(username):
    """Fetch user account by username (supports exact match and case-insensitive fallback)."""
    try:
        if not username:
            return None
        clean_u = str(username).strip()
        users_ref = db.collection("users")
        query = users_ref.where(filter=FieldFilter("username", "==", clean_u)).limit(1).stream()
        for doc in query:
            u = doc.to_dict()
            u["id"] = doc.id
            return u
        doc = users_ref.document(clean_u).get()
        if doc.exists:
            u = doc.to_dict()
            u["id"] = doc.id
            return u
        for d in users_ref.stream():
            u = d.to_dict()
            if (
                str(u.get("username", "")).strip().lower() == clean_u.lower()
                or str(d.id).strip().lower() == clean_u.lower()
            ):
                u["id"] = d.id
                return u
        return None
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_user_by_username: {e}")
        return None


def create_user(username, password, role="Staff", full_name=None, email=None, contact_no=None):
    """Create a new user account (Admin only)."""
    try:
        users_ref = db.collection("users")
        if users_ref.document(username).get().exists:
            return False, f"Username '{username}' already exists. Please choose a different one."

        query = users_ref.where(filter=FieldFilter("username", "==", username)).limit(1).stream()
        if len(list(query)) > 0:
            return False, f"Username '{username}' already exists. Please choose a different one."

        hashed_pwd = str(password).strip()
        if not hashed_pwd.startswith(("scrypt:", "pbkdf2:")):
            hashed_pwd = validators.hash_password(hashed_pwd)

        user_data = {
            "username": username,
            "password": hashed_pwd,
            "role": role,
            "full_name": full_name or "",
            "email": email or "",
            "contact_no": contact_no or "",
            "must_change_password": True if role == "Staff" else False,
            "created_at": now_pht(),
        }
        users_ref.document(username).set(user_data)
        return True, f"Account '{username}' ({role}) created successfully."
    except Exception as e:
        print(f"[FIRESTORE ERROR] create_user: {e}")
        return False, str(e)


def change_user_password(username, new_password):
    """Update a user's password in Firestore and mark must_change_password as False."""
    try:
        users_ref = db.collection("users")
        doc_ref = users_ref.document(username)
        hashed_pwd = str(new_password).strip()
        if not hashed_pwd.startswith(("scrypt:", "pbkdf2:")):
            hashed_pwd = validators.hash_password(hashed_pwd)

        update_payload = {
            "password": hashed_pwd,
            "must_change_password": False,
            "password_updated_at": now_pht(),
        }
        if doc_ref.get().exists:
            doc_ref.update(update_payload)
            return True, "Password updated successfully."

        query = users_ref.where(filter=FieldFilter("username", "==", username)).limit(1).stream()
        for doc in query:
            users_ref.document(doc.id).update(update_payload)
            return True, "Password updated successfully."

        return False, f"User '{username}' not found."
    except Exception as e:
        print(f"[FIRESTORE ERROR] change_user_password: {e}")
        return False, str(e)


def get_all_users():
    """Retrieve all user accounts."""
    try:
        users = []
        for d in db.collection("users").stream():
            u = d.to_dict()
            u["id"] = d.id
            users.append(u)
        return users
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_all_users: {e}")
        return []


def update_user(username, update_fields):
    """Update user personal and contact details."""
    try:
        if not username:
            return False, "Username is required."
        clean_u = str(username).strip()
        users_ref = db.collection("users")

        allowed_keys = {"full_name", "email", "contact_no", "role", "password", "must_change_password"}
        cleaned_data = {k: v for k, v in update_fields.items() if k in allowed_keys and v is not None}
        if "password" in cleaned_data:
            p_val = str(cleaned_data["password"]).strip()
            if not p_val.startswith(("scrypt:", "pbkdf2:")):
                cleaned_data["password"] = validators.hash_password(p_val)
        cleaned_data["updated_at"] = now_pht()

        doc_ref = users_ref.document(clean_u)
        if doc_ref.get().exists:
            doc_ref.update(cleaned_data)
            return True, f"User '{clean_u}' updated successfully."

        query = users_ref.where(filter=FieldFilter("username", "==", clean_u)).limit(1).stream()
        for doc in query:
            users_ref.document(doc.id).update(cleaned_data)
            return True, f"User '{clean_u}' updated successfully."

        for d in users_ref.stream():
            u = d.to_dict()
            if (
                str(u.get("username", "")).strip().lower() == clean_u.lower()
                or str(d.id).strip().lower() == clean_u.lower()
            ):
                users_ref.document(d.id).update(cleaned_data)
                return True, f"User '{clean_u}' updated successfully."

        return False, f"User '{clean_u}' not found."
    except Exception as e:
        print(f"[FIRESTORE ERROR] update_user: {e}")
        return False, str(e)


def delete_user(username):
    """Delete a user account from Firestore."""
    try:
        clean_u = str(username).strip()
        users_ref = db.collection("users")
        doc_ref = users_ref.document(clean_u)
        if doc_ref.get().exists:
            doc_ref.delete()
            return True, f"User '{clean_u}' removed successfully."

        query = users_ref.where(filter=FieldFilter("username", "==", clean_u)).limit(1).stream()
        for doc in query:
            users_ref.document(doc.id).delete()
            return True, f"User '{clean_u}' removed successfully."

        return False, f"User '{clean_u}' not found."
    except Exception as e:
        print(f"[FIRESTORE ERROR] delete_user: {e}")
        return False, str(e)


def get_mail_settings():
    """
    Retrieve email & notification settings with priority:
    1. Firestore 'settings/mail_settings'
    2. Firebase Secret Manager env vars (GMAIL_USER / GMAIL_APP_PASSWORD)
    3. Local config.json (local dev fallback only)
    """
    config_file_data = {}
    if not IS_CLOUD:
        for cfg_path in (
            os.path.join(BASE_DIR, "config.json"),
            os.path.abspath(os.path.join(BASE_DIR, "..", "Rebong Sancon Funeral Service", "config.json")),
        ):
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        config_file_data = json.load(f)
                        break
                except Exception:
                    pass

    settings = {
        "sender_email": (os.environ.get("GMAIL_USER") or config_file_data.get("GMAIL_USER") or "").strip(),
        "sender_password": (os.environ.get("GMAIL_APP_PASSWORD") or config_file_data.get("GMAIL_APP_PASSWORD") or "").strip(),
        "admin_notification_email": "",
        "notifications_enabled": True,
    }

    try:
        doc = db.collection("settings").document("mail_settings").get()
        if doc.exists:
            stored = doc.to_dict() or {}
            for k in ("sender_email", "sender_password", "admin_notification_email"):
                if stored.get(k) and str(stored.get(k)).strip():
                    settings[k] = str(stored.get(k)).strip()
            if "notifications_enabled" in stored:
                settings["notifications_enabled"] = bool(stored["notifications_enabled"])
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_mail_settings: {e}")

    return settings


def save_mail_settings(new_settings):
    """Persist email & notification configurations in Firestore (skip config.json writes in cloud)."""
    try:
        settings_ref = db.collection("settings").document("mail_settings")
        clean_data = {
            "sender_email": (new_settings.get("sender_email") or "").strip(),
            "sender_password": (new_settings.get("sender_password") or "").strip(),
            "admin_notification_email": (new_settings.get("admin_notification_email") or "").strip(),
            "notifications_enabled": bool(new_settings.get("notifications_enabled", True)),
            "updated_at": now_pht(),
        }
        settings_ref.set(clean_data, merge=True)

        if not IS_CLOUD:
            config_path = os.path.join(BASE_DIR, "config.json")
            try:
                config_data = {}
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                if clean_data.get("sender_email"):
                    config_data["GMAIL_USER"] = clean_data["sender_email"]
                if clean_data.get("sender_password"):
                    config_data["GMAIL_APP_PASSWORD"] = clean_data["sender_password"]
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=4)
            except Exception as ce:
                print(f"[CONFIG SYNC WARNING] {ce}")

        return True, "Email settings saved successfully."
    except Exception as e:
        print(f"[FIRESTORE ERROR] save_mail_settings: {e}")
        return False, str(e)


def get_admin_emails():
    """Return list of all verified Admin email addresses configured in the system."""
    emails = []
    try:
        settings = get_mail_settings()
        admin_setting_email = (settings.get("admin_notification_email") or "").strip()
        if admin_setting_email and "@" in admin_setting_email:
            emails.append(admin_setting_email)

        for u in get_all_users():
            if u.get("role") == "Admin":
                em = (u.get("email") or "").strip()
                if em and "@" in em and em not in emails:
                    emails.append(em)

        real_emails = [e for e in emails if not e.endswith("@rebongsancon.com")]
        valid_candidates = real_emails if real_emails else emails

        if not valid_candidates:
            sender = (settings.get("sender_email") or "").strip()
            if sender and "@" in sender and not sender.endswith("@rebongsancon.com"):
                valid_candidates = [sender]

        clean_results = []
        for e in valid_candidates:
            is_valid, clean_e, _ = validators.validate_email(e)
            if is_valid and clean_e not in clean_results:
                clean_results.append(clean_e)

        return clean_results
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_admin_emails: {e}")
        return []


# =====================================================================
# 4. CASKETS & PACKAGES INVENTORY (FIRESTORE)
# =====================================================================
def get_caskets():
    """Fetch all casket packages from Firestore with cloud-safe QR data."""
    try:
        caskets = [_normalize_casket_doc(doc) for doc in db.collection("caskets").stream()]
        caskets.sort(key=lambda x: str(x.get("casket_code", "")))
        return caskets
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_caskets: {e}")
        return []


def get_casket_by_code(casket_code):
    """Fetch a single casket by its QR / Casket code or ID."""
    try:
        clean_code = (casket_code or "").strip()
        if not clean_code:
            return None

        doc = db.collection("caskets").document(clean_code).get()
        if doc.exists:
            return _normalize_casket_doc(doc)

        for field_name in ("casket_code", "qr_code"):
            query = db.collection("caskets").where(filter=FieldFilter(field_name, "==", clean_code)).limit(1).stream()
            for d in query:
                return _normalize_casket_doc(d)

        return None
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_casket_by_code: {e}")
        return None


def add_casket(data):
    """Create or update a casket package in Firestore with unique in-memory QR code."""
    try:
        code = (data.get("casket_code") or "").strip().upper()
        if not code:
            return False

        inclusions = data.get("inclusions", [])
        if isinstance(inclusions, str):
            try:
                inclusions = json.loads(inclusions)
            except Exception:
                inclusions = [i.strip() for i in inclusions.split("\n") if i.strip()]

        qr_url, qr_b64 = generate_package_qr(code)

        pkg_data = {
            "casket_code": code,
            "qr_code": code,
            "qr_image_url": qr_url,
            "qr_base64": qr_b64,
            "name": data.get("name", "Funeral Package"),
            "category": data.get("category", "Standard Casket Package"),
            "material": data.get("material", "Hardwood"),
            "dimensions": data.get("dimensions", "Standard Full Size"),
            "base_price": float(data.get("base_price", 0)),
            "stock": int(data.get("stock", 1)),
            "inclusions": inclusions,
            "image_url": data.get("image_url") or None,
            "updated_at": now_pht(),
        }

        doc_ref = db.collection("caskets").document(code)
        if not doc_ref.get().exists:
            pkg_data["created_at"] = now_pht()

        doc_ref.set(pkg_data, merge=True)
        return True
    except Exception as e:
        print(f"[FIRESTORE ERROR] add_casket: {e}")
        return False


def update_casket(casket_code, data):
    """Update existing casket package details and refresh unique QR code."""
    try:
        code = casket_code.strip().upper()
        doc_ref = db.collection("caskets").document(code)

        inclusions = data.get("inclusions")
        if isinstance(inclusions, str):
            try:
                inclusions = json.loads(inclusions)
            except Exception:
                inclusions = [i.strip() for i in inclusions.split("\n") if i.strip()]

        qr_url, qr_b64 = generate_package_qr(code)

        update_fields = {
            "casket_code": code,
            "qr_code": code,
            "qr_image_url": qr_url,
            "qr_base64": qr_b64,
            "name": data.get("name"),
            "category": data.get("category", "Standard Casket Package"),
            "material": data.get("material", "Hardwood"),
            "dimensions": data.get("dimensions", "Standard Full Size"),
            "base_price": float(data.get("base_price", 0)),
            "stock": int(data.get("stock", 1)),
            "updated_at": now_pht(),
        }
        if inclusions is not None:
            update_fields["inclusions"] = inclusions
        if data.get("image_url"):
            update_fields["image_url"] = data.get("image_url")

        doc_ref.set(update_fields, merge=True)
        return True
    except Exception as e:
        print(f"[FIRESTORE ERROR] update_casket: {e}")
        return False


def quick_update_casket(casket_code, base_price, stock):
    """Quickly update price and stock in Firestore."""
    try:
        code = casket_code.strip().upper()
        db.collection("caskets").document(code).update({
            "base_price": float(base_price),
            "stock": int(stock),
            "updated_at": now_pht(),
        })
        return True, f"Price and stock for {code} updated successfully."
    except Exception as e:
        print(f"[FIRESTORE ERROR] quick_update_casket: {e}")
        return False, str(e)


def delete_casket(casket_code):
    """Delete a casket package from Firestore."""
    try:
        code = casket_code.strip().upper()
        db.collection("caskets").document(code).delete()
        return True
    except Exception as e:
        print(f"[FIRESTORE ERROR] delete_casket: {e}")
        return False


# =====================================================================
# 5. ORDERS & BILLING (FIRESTORE)
# =====================================================================
def create_order(payload):
    """Insert a new POS checkout order (Package or Custom) into Firestore orders & reservations."""
    try:
        orders_ref = db.collection("orders")
        now_dt = now_pht()
        year_num = now_dt.year

        all_docs = list(orders_ref.select(["invoice_no"]).stream())
        count = len(all_docs) + 90
        invoice_no = f"INV-{year_num}-{count:04d}"
        while orders_ref.document(invoice_no).get().exists:
            count += 1
            invoice_no = f"INV-{year_num}-{count:04d}"

        order_type = payload.get("orderType") or payload.get("order_type") or "Package"
        cust = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
        pkg = payload.get("scannedPackage") if isinstance(payload.get("scannedPackage"), dict) else {}
        pricing = payload.get("pricing") if isinstance(payload.get("pricing"), dict) else {}
        custom_details = payload.get("customDetails") or payload.get("custom_details")

        deceased_name = (
            cust.get("deceasedName") or cust.get("deceased_name")
            or payload.get("deceased_name") or payload.get("deceasedName")
            or payload.get("deceased") or "Deceased Client"
        )
        contact_person = (
            cust.get("contactPerson") or cust.get("contact_person")
            or payload.get("contact_person") or payload.get("contactPerson")
            or payload.get("client_name") or payload.get("clientName") or "Family Rep"
        )
        contact_number = (
            cust.get("contactNumber") or cust.get("contact_number")
            or payload.get("contact_number") or payload.get("contactNumber")
            or payload.get("mobile") or payload.get("phone") or ""
        )

        if order_type == "Custom":
            casket_code = (
                payload.get("casketCode") or payload.get("casket_code")
                or (custom_details.get("casket", {}).get("code") if custom_details else "CUSTOM")
            )
            casket_name = payload.get("casketName") or payload.get("casket_name") or "Custom Funeral Service"
        else:
            casket_code = (
                pkg.get("code") or pkg.get("casketCode")
                or payload.get("casket_code") or payload.get("casketCode")
                or payload.get("code") or "CSK-OAK-01"
            )
            casket_name = (
                pkg.get("name") or pkg.get("casketName")
                or payload.get("casket_name") or payload.get("casketName") or "Casket Package"
            )

        base_price = float(pricing.get("basePrice") or payload.get("base_price") or payload.get("basePrice") or 0.0)
        addons_total = float(pricing.get("addOnsTotal") or payload.get("addons_total") or payload.get("addOnsTotal") or 0.0)
        subtotal = float(pricing.get("subtotal") or payload.get("subtotal") or (base_price + addons_total))
        discount_amount = float(pricing.get("discountAmount") or payload.get("discount_amount") or payload.get("discountAmount") or 0.0)
        total_amount = float(pricing.get("totalAmount") or payload.get("total_amount") or payload.get("totalAmount") or max(0.0, subtotal - discount_amount))
        down_payment = float(pricing.get("downPayment") or payload.get("down_payment") or payload.get("downPayment") or 0.0)
        remaining_balance = float(pricing.get("remainingBalance") or payload.get("remaining_balance") or payload.get("remainingBalance") or max(0.0, total_amount - down_payment))
        payment_method = str(pricing.get("paymentMethod") or payload.get("payment_method") or payload.get("paymentMethod") or "Cash").strip()

        order_doc = {
            "invoice_no": invoice_no,
            "order_type": order_type,
            "deceased_name": deceased_name,
            "contact_person": contact_person,
            "contact_number": contact_number,
            "casket_code": casket_code,
            "casket_name": casket_name,
            "custom_details": custom_details,
            "base_price": base_price,
            "addons_total": addons_total,
            "subtotal": subtotal,
            "senior_discount_applied": bool(pricing.get("seniorDiscountApplied") or payload.get("senior_discount_applied")),
            "discount_amount": discount_amount,
            "total_amount": total_amount,
            "down_payment": down_payment,
            "remaining_balance": remaining_balance,
            "payment_method": payment_method,
            "payment_status": "Fully Settled" if remaining_balance <= 0 else "Partial (Deposit)",
            "order_status": "Active",
            "is_archived": False,
            "archived_at": None,
            "addons": payload.get("addOns") or payload.get("addons") or [],
            "created_at": now_dt,
        }

        orders_ref.document(invoice_no).set(order_doc)

        if deceased_name:
            res_code = f"RES-{count + 15}"
            while db.collection("reservations").document(res_code).get().exists:
                count += 1
                res_code = f"RES-{count + 15}"

            today_str = now_dt.strftime("%Y-%m-%d")
            chapel_room = "Chapel A (St. Peter Suite)"
            hearse_driver = "Kuya Danny (Black Glass Hearse)"
            if custom_details:
                ch_info = custom_details.get("chapel", {})
                if ch_info and ch_info.get("name"):
                    chapel_room = ch_info.get("name")
                hr_info = custom_details.get("hearse", {})
                if hr_info and hr_info.get("name"):
                    hearse_driver = hr_info.get("name")

            res_doc = {
                "reservation_code": res_code,
                "invoice_no": invoice_no,
                "deceased_name": deceased_name,
                "contact_person": contact_person,
                "contact_number": contact_number,
                "chapel_room": chapel_room,
                "hearse_driver": hearse_driver,
                "start_date": today_str,
                "end_date": today_str,
                "interment_date": "Pending Schedule",
                "cemetery": "Victoria Public Cemetery",
                "status": "Active",
                "created_at": now_dt,
            }
            db.collection("reservations").document(res_code).set(res_doc)

        if casket_code and casket_code not in ("CUSTOM", "NONE", "CLIENT"):
            try:
                casket_ref = db.collection("caskets").document(casket_code)
                casket_snap = casket_ref.get()
                if casket_snap.exists:
                    cur_stock = int(casket_snap.to_dict().get("stock", 0))
                    casket_ref.update({"stock": max(0, cur_stock - 1)})
            except Exception as stk_err:
                print(f"[FIRESTORE STOCK UPDATE WARNING]: {stk_err}")

        return invoice_no
    except Exception as e:
        print(f"[FIRESTORE ERROR] create_order: {e}")
        raise e


def get_orders(include_archived=False, only_archived=False, order_type=None):
    """Retrieve orders with optional archive and type filtering."""
    try:
        orders = []
        for doc in db.collection("orders").stream():
            r = doc.to_dict()
            r["id"] = doc.id
            is_arch = bool(r.get("is_archived", False))

            if only_archived and not is_arch:
                continue
            if not include_archived and not only_archived and is_arch:
                continue

            otype = r.get("order_type", "Package")
            if order_type and order_type != "all" and otype != order_type:
                continue

            r["created_at_str"] = _format_datetime(r.get("created_at"))
            r["archived_at_str"] = _format_datetime(r.get("archived_at")) if r.get("archived_at") else "--"

            r["base_price"] = _safe_float(r.get("base_price", 0))
            r["total_amount"] = _safe_float(r.get("total_amount", 0))
            r["discount_amount"] = _safe_float(r.get("discount_amount", 0))
            raw_sub = _safe_float(r.get("subtotal", 0))
            r["subtotal"] = raw_sub if raw_sub > 0 else round(r["total_amount"] + r["discount_amount"], 2)
            r["down_payment"] = _safe_float(r.get("down_payment", 0))
            r["remaining_balance"] = _safe_float(r.get("remaining_balance", 0))
            r["overpayment_refund_due"] = _safe_float(r.get("overpayment_refund_due", 0))
            r["discount_reason"] = r.get("discount_reason") or ""
            r["discount_applied_by"] = r.get("discount_applied_by") or ""
            r["discount_applied_at"] = r.get("discount_applied_at") or ""
            orders.append(r)

        orders.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        return orders
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_orders: {e}")
        return []


def get_order_by_invoice(invoice_no):
    """Retrieve single order document by invoice number."""
    try:
        doc = db.collection("orders").document(invoice_no).get()
        if not doc.exists:
            q = db.collection("orders").where(filter=FieldFilter("invoice_no", "==", invoice_no)).limit(1).stream()
            docs = list(q)
            if not docs:
                return None
            doc = docs[0]

        order = doc.to_dict()
        order["id"] = doc.id
        order["created_at_str"] = _format_datetime(order.get("created_at"))
        order["archived_at_str"] = _format_datetime(order.get("archived_at"))
        order["total_amount"] = _safe_float(order.get("total_amount", 0))
        order["discount_amount"] = _safe_float(order.get("discount_amount", 0))
        raw_sub = _safe_float(order.get("subtotal", 0))
        order["subtotal"] = raw_sub if raw_sub > 0 else round(order["total_amount"] + order["discount_amount"], 2)
        order["down_payment"] = _safe_float(order.get("down_payment", 0))
        order["remaining_balance"] = _safe_float(order.get("remaining_balance", 0))
        order["overpayment_refund_due"] = _safe_float(order.get("overpayment_refund_due", 0))
        return order
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_order_by_invoice: {e}")
        return None


def update_order_status(invoice_no, new_status):
    """Update order_status (e.g., 'Completed', 'Active')."""
    try:
        doc_ref = db.collection("orders").document(invoice_no)
        if not doc_ref.get().exists:
            return False, "Order not found"
        doc_ref.update({"order_status": new_status})
        return True, f"Order status updated to '{new_status}'."
    except Exception as e:
        print(f"[FIRESTORE ERROR] update_order_status: {e}")
        return False, str(e)


def archive_order(invoice_no):
    """Archive an order that is completed (Admin only)."""
    try:
        doc_ref = db.collection("orders").document(invoice_no)
        if not doc_ref.get().exists:
            return False, "Order not found"
        doc_ref.update({
            "is_archived": True,
            "archived_at": now_pht(),
            "order_status": "Completed",
        })
        return True, f"Order {invoice_no} has been archived successfully."
    except Exception as e:
        print(f"[FIRESTORE ERROR] archive_order: {e}")
        return False, str(e)


def restore_order(invoice_no):
    """Restore an archived order back to active list (Admin only)."""
    try:
        doc_ref = db.collection("orders").document(invoice_no)
        if not doc_ref.get().exists:
            return False, "Order not found"
        doc_ref.update({"is_archived": False, "archived_at": None})
        return True, f"Order {invoice_no} has been restored successfully."
    except Exception as e:
        print(f"[FIRESTORE ERROR] restore_order: {e}")
        return False, str(e)


def delete_order_permanently(invoice_no):
    """Permanently delete an order and associated reservations from Firestore (Admin only)."""
    try:
        db.collection("orders").document(invoice_no).delete()
        res_docs = db.collection("reservations").where(filter=FieldFilter("invoice_no", "==", invoice_no)).stream()
        for r in res_docs:
            db.collection("reservations").document(r.id).delete()
        return True, f"Order {invoice_no} has been permanently deleted from the database."
    except Exception as e:
        print(f"[FIRESTORE ERROR] delete_order_permanently: {e}")
        return False, str(e)


def get_order_counts():
    """Get counts for active all, package, custom, and archived orders."""
    try:
        all_active = 0
        package_count = 0
        custom_count = 0
        archived_count = 0

        for d in db.collection("orders").stream():
            data = d.to_dict()
            if bool(data.get("is_archived", False)):
                archived_count += 1
            else:
                all_active += 1
                if data.get("order_type", "Package") == "Custom":
                    custom_count += 1
                else:
                    package_count += 1

        return {
            "all": all_active,
            "package": package_count,
            "custom": custom_count,
            "archived": archived_count,
        }
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_order_counts: {e}")
        return {"all": 0, "package": 0, "custom": 0, "archived": 0}


def _find_order_doc(invoice_no):
    """Locate an order document reference and snapshot by doc ID or invoice_no field."""
    doc_ref = db.collection("orders").document(invoice_no)
    doc = doc_ref.get()
    if doc.exists:
        return doc_ref, doc
    docs = list(db.collection("orders").where(filter=FieldFilter("invoice_no", "==", invoice_no)).limit(1).stream())
    if docs:
        return docs[0].reference, docs[0]
    return None, None


def update_payment(invoice_no, add_payment):
    """Add payment amount, reduce remaining balance, and update status."""
    try:
        doc_ref, doc = _find_order_doc(invoice_no)
        if not doc_ref or not doc:
            return False, "Order not found"

        order = doc.to_dict()
        new_down = round(_safe_float(order.get("down_payment", 0)) + float(add_payment), 2)
        new_rem = round(max(0.0, _safe_float(order.get("remaining_balance", 0)) - float(add_payment)), 2)
        new_status = "Fully Settled" if new_rem <= 0 else "Partial (Deposit)"

        doc_ref.update({
            "down_payment": new_down,
            "remaining_balance": new_rem,
            "payment_status": new_status,
        })
        return True, "Payment updated successfully"
    except Exception as e:
        print(f"[FIRESTORE ERROR] update_payment: {e}")
        return False, str(e)


def apply_order_discount(invoice_no, discount_type="percent", discount_value=0.0, reason="", applied_by="Admin"):
    """Retroactively apply or update a discount on any existing order."""
    try:
        doc_ref, doc = _find_order_doc(invoice_no)
        if not doc_ref or not doc:
            return False, "Order not found", None

        order = doc.to_dict()
        old_total = _safe_float(order.get("total_amount", 0))
        old_discount = _safe_float(order.get("discount_amount", 0))
        raw_subtotal = _safe_float(order.get("subtotal", 0))

        base_subtotal = raw_subtotal if raw_subtotal > 0 else round(old_total + old_discount, 2)
        if base_subtotal <= 0:
            base_subtotal = _safe_float(order.get("base_price", 0))
        if base_subtotal <= 0:
            return False, "Cannot apply discount to an order with ₱0 base subtotal.", None

        val = float(discount_value)
        if val < 0:
            return False, "Discount value cannot be negative.", None

        if discount_type == "percent":
            if val > 100:
                return False, "Discount percentage cannot exceed 100%.", None
            new_discount = round((base_subtotal * val) / 100.0, 2)
            discount_percent = round(val, 2)
        elif discount_type == "fixed":
            if val > base_subtotal + 0.01:
                return False, f"Fixed discount (₱{val:,.2f}) cannot exceed original subtotal (₱{base_subtotal:,.2f}).", None
            new_discount = round(min(val, base_subtotal), 2)
            discount_percent = round((new_discount / base_subtotal) * 100.0, 2)
        else:
            return False, "Invalid discount type. Use 'percent' or 'fixed'.", None

        new_total = round(max(0.0, base_subtotal - new_discount), 2)
        down_payment = _safe_float(order.get("down_payment", 0))
        new_remaining = round(max(0.0, new_total - down_payment), 2)
        overpayment_refund = round(max(0.0, down_payment - new_total), 2)
        new_status = "Fully Settled" if new_remaining <= 0 else "Partial (Deposit)"

        now_str = now_pht().strftime("%b %d, %Y • %I:%M %p")
        clean_reason = str(reason or "").strip()[:200]

        update_data = {
            "subtotal": base_subtotal,
            "discount_amount": new_discount,
            "discount_percent": discount_percent,
            "senior_discount_applied": new_discount > 0,
            "discount_reason": clean_reason if new_discount > 0 else "",
            "discount_applied_by": applied_by if new_discount > 0 else "",
            "discount_applied_at": now_str if new_discount > 0 else "",
            "total_amount": new_total,
            "remaining_balance": new_remaining,
            "overpayment_refund_due": overpayment_refund,
            "payment_status": new_status,
        }
        doc_ref.update(update_data)

        msg = (
            f"Discount of ₱{new_discount:,.2f} ({discount_percent:.1f}%) applied to {invoice_no}. "
            f"New Total: ₱{new_total:,.2f} | Remaining Balance: ₱{new_remaining:,.2f}."
        )
        if overpayment_refund > 0:
            msg += f" Note: Client has already paid ₱{down_payment:,.2f} (Refund/Credit Due: ₱{overpayment_refund:,.2f})."
        return True, msg, update_data
    except Exception as e:
        print(f"[FIRESTORE ERROR] apply_order_discount: {e}")
        return False, str(e), None


# =====================================================================
# 6. RESERVATIONS (FIRESTORE)
# =====================================================================
def get_reservations():
    """Retrieve all chapel reservations from Firestore."""
    try:
        res = []
        for d in db.collection("reservations").stream():
            r = d.to_dict()
            r["id"] = d.id
            res.append(r)
        res.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        return res
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_reservations: {e}")
        return []


def update_reservation_venue(reservation_code, new_venue):
    """Update chapel room for a reservation."""
    try:
        doc_ref = db.collection("reservations").document(reservation_code)
        if not doc_ref.get().exists:
            return False, "Reservation not found"
        doc_ref.update({"chapel_room": new_venue})
        return True, "Venue updated successfully"
    except Exception as e:
        print(f"[FIRESTORE ERROR] update_reservation_venue: {e}")
        return False, str(e)


# =====================================================================
# 7. STAFF ATTENDANCE (FIRESTORE - ASIA/MANILA 12-HOUR FORMAT)
# =====================================================================
def record_staff_time_in(username, user_id=None):
    """Record Time In for staff today in Philippine Time (12-hour format)."""
    try:
        now_dt = now_pht()
        today_str = now_dt.strftime("%Y-%m-%d")
        now_time_12 = now_dt.strftime("%I:%M:%S %p")
        now_time_24 = now_dt.strftime("%H:%M:%S")

        att_ref = db.collection("staff_attendance")
        docs = (
            att_ref.where(filter=FieldFilter("username", "==", username))
            .where(filter=FieldFilter("log_date", "==", today_str))
            .stream()
        )
        for d in docs:
            record = d.to_dict()
            if record.get("time_in") and not record.get("time_out"):
                t_display = _format_time_12hr(record.get("time_in"))
                return {"success": False, "message": f"Already timed in today at {t_display}."}

        doc_id = f"{username}_{today_str}_{int(now_dt.timestamp())}"
        new_record = {
            "user_id": user_id,
            "username": username,
            "log_date": today_str,
            "time_in": now_time_12,
            "time_in_24": now_time_24,
            "time_out": None,
            "duration_hours": 0.0,
            "status": "On Duty",
            "created_at": now_dt,
        }
        att_ref.document(doc_id).set(new_record)
        return {
            "success": True,
            "time_in": now_time_12,
            "log_date": today_str,
            "message": f"Time In recorded at {now_time_12}",
            "record": new_record,
        }
    except Exception as e:
        print(f"[FIRESTORE ERROR] record_staff_time_in: {e}")
        return {"success": False, "message": str(e)}


def record_staff_time_out(username):
    """Record Time Out for staff today in Philippine Time (12-hour format)."""
    try:
        now_dt = now_pht()
        today_str = now_dt.strftime("%Y-%m-%d")
        now_time_12 = now_dt.strftime("%I:%M:%S %p")
        now_time_24 = now_dt.strftime("%H:%M:%S")

        att_ref = db.collection("staff_attendance")
        docs = list(
            att_ref.where(filter=FieldFilter("username", "==", username))
            .where(filter=FieldFilter("log_date", "==", today_str))
            .stream()
        )

        target_doc = None
        for d in docs:
            rec = d.to_dict()
            if rec.get("time_in") and not rec.get("time_out"):
                target_doc = d
                break

        if not target_doc:
            return {"success": False, "message": "No active Time In found for today."}

        rec = target_doc.to_dict()
        t_in_str = rec.get("time_in")
        t_in_24 = rec.get("time_in_24")
        duration_hours = 0.0

        t_in_parsed = None
        for cand in (t_in_24, t_in_str):
            if not cand:
                continue
            for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
                try:
                    t_in_parsed = datetime.strptime(str(cand).strip(), fmt).time()
                    break
                except ValueError:
                    pass
            if t_in_parsed:
                break

        if t_in_parsed:
            try:
                in_seconds = t_in_parsed.hour * 3600 + t_in_parsed.minute * 60 + t_in_parsed.second
                now_seconds = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
                duration_hours = round(max(0, (now_seconds - in_seconds) / 3600.0), 2)
            except Exception:
                duration_hours = 0.0

        att_ref.document(target_doc.id).update({
            "time_out": now_time_12,
            "time_out_24": now_time_24,
            "duration_hours": duration_hours,
            "status": "Completed",
        })
        updated_record = dict(rec)
        updated_record.update({
            "time_out": now_time_12,
            "time_out_24": now_time_24,
            "duration_hours": duration_hours,
            "status": "Completed",
        })
        return {
            "success": True,
            "time_in": rec.get("time_in"),
            "time_out": now_time_12,
            "duration": duration_hours,
            "log_date": today_str,
            "message": f"Time Out recorded at {now_time_12}",
            "record": updated_record,
        }
    except Exception as e:
        print(f"[FIRESTORE ERROR] record_staff_time_out: {e}")
        return {"success": False, "message": str(e)}


def get_today_attendance(username):
    """Get active or today's attendance record for staff (Philippine Time)."""
    try:
        today_str = now_pht().strftime("%Y-%m-%d")
        docs = (
            db.collection("staff_attendance")
            .where(filter=FieldFilter("username", "==", username))
            .where(filter=FieldFilter("log_date", "==", today_str))
            .stream()
        )
        for d in docs:
            rec = d.to_dict()
            rec["id"] = d.id
            rec["time_in_str"] = _format_time_12hr(rec.get("time_in"))
            rec["time_out_str"] = _format_time_12hr(rec.get("time_out")) if rec.get("time_out") else None
            return rec
        return None
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_today_attendance: {e}")
        return None


def get_all_attendance_logs(filter_date=None, search_user=None):
    """Retrieve attendance logs with 12-hour formatted time and optional filter."""
    try:
        logs = []
        for d in db.collection("staff_attendance").stream():
            rec = d.to_dict()
            rec["id"] = d.id

            if filter_date and rec.get("log_date") != filter_date:
                continue
            if search_user and search_user.lower() not in str(rec.get("username", "")).lower():
                continue

            rec["time_in_str"] = _format_time_12hr(rec.get("time_in"))
            rec["time_out_str"] = _format_time_12hr(rec.get("time_out")) if rec.get("time_out") else None
            logs.append(rec)

        logs.sort(key=lambda x: (str(x.get("log_date", "")), str(x.get("time_in", ""))), reverse=True)
        return logs
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_all_attendance_logs: {e}")
        return []


def get_staff_attendance_history(username):
    """Retrieve attendance history for a single staff member in 12-hour format."""
    try:
        history = []
        for d in db.collection("staff_attendance").where(filter=FieldFilter("username", "==", username)).stream():
            rec = d.to_dict()
            rec["id"] = d.id
            rec["time_in_str"] = _format_time_12hr(rec.get("time_in"))
            rec["time_out_str"] = _format_time_12hr(rec.get("time_out")) if rec.get("time_out") else None
            history.append(rec)

        history.sort(key=lambda x: (str(x.get("log_date", "")), str(x.get("time_in", ""))), reverse=True)
        return history[:30]
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_staff_attendance_history: {e}")
        return []


# =====================================================================
# 8. ESP32 SCAN LOGS & ACTIVE SCAN SYNC (FIRESTORE)
# =====================================================================
def log_esp32_scan(qr_code, ip_address="127.0.0.1", package_data=None):
    """Record an ESP32/Mobile scan event and update esp32_active_scan/latest in Firestore."""
    try:
        now_dt = now_pht()
        now_time_12 = now_dt.strftime("%b %d, %Y • %I:%M:%S %p")
        log_entry = {
            "qr_code": qr_code,
            "ip_address": ip_address,
            "scanned_at": now_dt,
            "scanned_at_str": now_time_12,
        }
        if package_data:
            log_entry["package_name"] = package_data.get("name")
            log_entry["base_price"] = package_data.get("basePrice") or package_data.get("base_price")

        db.collection("esp32_scan_logs").add(log_entry)

        scan_payload = {
            "qr_code": qr_code,
            "casket_code": qr_code,
            "timestamp": now_dt,
            "timestamp_str": now_time_12,
            "package": package_data or {},
        }
        db.collection("esp32_active_scan").document("latest").set(scan_payload)
    except Exception as e:
        print(f"[FIRESTORE ERROR] log_esp32_scan: {e}")


def get_active_scan():
    """Fetch the latest active scan document directly from Firestore (stateless across instances)."""
    try:
        doc_snap = db.collection("esp32_active_scan").document("latest").get()
        if not doc_snap.exists:
            return None
        return doc_snap.to_dict() or {}
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_active_scan: {e}")
        return None


def clear_active_scan():
    """Delete esp32_active_scan/latest from Firestore."""
    try:
        db.collection("esp32_active_scan").document("latest").delete()
    except Exception as e:
        print(f"[FIRESTORE ERROR] clear_active_scan: {e}")


# =====================================================================
# 9. ESP32 LIVE DISPLAY QR SYNC (FIRESTORE)
# =====================================================================
def save_active_display_qr(qr_data, uploaded_by="Admin"):
    """Save active dynamic QR to Firestore (esp32_display_qr/current) and log history."""
    try:
        now_time_12 = now_pht().strftime("%b %d, %Y • %I:%M:%S %p")
        record = {
            "has_qr": True,
            "qr_id": qr_data.get("qr_id", ""),
            "qr_text": qr_data.get("qr_text", ""),
            "title": qr_data.get("title", ""),
            "amount": qr_data.get("amount", ""),
            "amount_display": qr_data.get("amount_display", ""),
            "subtitle": qr_data.get("subtitle", ""),
            "width": qr_data.get("width", 64),
            "height": qr_data.get("height", 64),
            "invert": bool(qr_data.get("invert", True)),
            "bitmap_hex": qr_data.get("bitmap_hex", ""),
            "preview_url": qr_data.get("preview_url", ""),
            "timestamp": now_time_12,
            "uploaded_by": uploaded_by,
            "displayed_on_hardware": False,
            "hardware_ip": "",
            "hardware_device": "",
            "hardware_ack_at": None,
        }
        db.collection("esp32_display_qr").document("current").set(record)
        db.collection("esp32_display_qr_logs").add(record)
        return record
    except Exception as e:
        print(f"[FIRESTORE ERROR] save_active_display_qr: {e}")
        return qr_data


def get_active_display_qr():
    """Fetch the currently active dynamic QR from Firestore."""
    try:
        doc = db.collection("esp32_display_qr").document("current").get()
        if doc.exists:
            return _sanitize_dict(doc.to_dict())
    except Exception as e:
        print(f"[FIRESTORE ERROR] get_active_display_qr: {e}")
    return None


def clear_active_display_qr():
    """Reset active QR document in Firestore to standby mode."""
    try:
        standby_record = {
            "has_qr": False,
            "qr_id": "",
            "qr_text": "",
            "title": "SYSTEM READY",
            "amount": "",
            "amount_display": "",
            "subtitle": "Standby Mode",
            "width": 64,
            "height": 64,
            "invert": True,
            "bitmap_hex": "",
            "preview_url": "",
            "timestamp": now_pht().strftime("%b %d, %Y • %I:%M:%S %p"),
            "displayed_on_hardware": False,
            "hardware_ip": "",
            "hardware_device": "",
            "hardware_ack_at": None,
        }
        db.collection("esp32_display_qr").document("current").set(standby_record)
        return standby_record
    except Exception as e:
        print(f"[FIRESTORE ERROR] clear_active_display_qr: {e}")
        return None


def ack_hardware_qr_displayed(qr_id, ip_address="127.0.0.1", device_name="ESP32"):
    """Called when ESP32 finishes drawing the QR on its screen."""
    try:
        now_time_12 = now_pht().strftime("%b %d, %Y • %I:%M:%S %p")
        doc_ref = db.collection("esp32_display_qr").document("current")
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            if data.get("qr_id") == qr_id or not qr_id:
                doc_ref.update({
                    "displayed_on_hardware": True,
                    "hardware_ip": ip_address,
                    "hardware_device": device_name,
                    "hardware_ack_at": now_time_12,
                })
                return True
    except Exception as e:
        print(f"[FIRESTORE ERROR] ack_hardware_qr_displayed: {e}")
    return False


def get_connection():
    """Backwards-compatibility dummy for legacy SQL callers."""
    class MockCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args, **kwargs): pass
        def fetchone(self): return None
        def fetchall(self): return []

    class MockConnection:
        def cursor(self): return MockCursor()
        def close(self): pass

    return MockConnection()
