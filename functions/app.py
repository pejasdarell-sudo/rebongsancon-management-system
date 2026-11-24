import os
import hmac
import json
import time
import calendar
import smtplib
from collections import defaultdict
from datetime import datetime
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from flask import Flask, Response, render_template, request, jsonify, redirect, url_for, session, flash
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import notification_service
import validators
import esp32_qr_service
import qr_generator

# =====================================================================
# FLASK APP INITIALIZATION (FIREBASE HOSTING + CLOUD FUNCTIONS 2ND GEN)
# =====================================================================
IS_CLOUD = bool(os.environ.get("K_SERVICE") or os.environ.get("FUNCTION_TARGET"))

# Serve ../public/static during local dev; in Firebase Cloud, Hosting CDN serves /static/** directly
_PUBLIC_STATIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public", "static"))
_LOCAL_STATIC = os.path.join(os.path.dirname(__file__), "static")
_STATIC_DIR = _PUBLIC_STATIC if os.path.isdir(_PUBLIC_STATIC) else _LOCAL_STATIC

app = Flask(__name__, static_folder=_STATIC_DIR, static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# CRITICAL for Firebase Hosting:
# Firebase Hosting strips ALL cookies except `__session` before forwarding to Cloud Functions.
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or "rebongsancon-local-dev-secret-change-me"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max payload limit
app.config["SESSION_COOKIE_NAME"] = "__session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_CLOUD


@app.after_request
def apply_security_headers(response):
    """Inject HTTP security headers and prevent Firebase Hosting CDN from caching private pages."""
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Keep static assets and generated QR images cacheable; force private/no-store on all dynamic routes
    if not request.path.startswith("/static/") and not request.path.startswith("/api/qr/"):
        response.headers["Cache-Control"] = "private, no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


def save_uploaded_image(file, folder="caskets", prefix=""):
    """
    Validate image with Pillow and upload persistently to Firebase Storage.
    Falls back to local static/uploads only when running locally without Storage.
    """
    if not file or not file.filename:
        return None
    valid, safe_name, err = validators.validate_image_file(file)
    if not valid:
        print(f"[IMAGE VALIDATION ERROR] {err}")
        return None

    unique_name = f"{prefix}{int(time.time())}_{safe_name}"

    storage_url = db.upload_image_to_storage(file, folder=folder, unique_name=unique_name)
    if storage_url:
        return storage_url

    if IS_CLOUD:
        print("[IMAGE UPLOAD ERROR] Firebase Storage upload failed in cloud runtime.")
        return None

    local_dir = os.path.join(_STATIC_DIR, "uploads", folder)
    os.makedirs(local_dir, exist_ok=True)
    filepath = os.path.join(local_dir, unique_name)
    file.seek(0)
    file.save(filepath)
    return f"/static/uploads/{folder}/{unique_name}"


def get_esp32_ip():
    """Retrieve configured ESP32 IP from env variable or local config.json (or Cloud label)."""
    env_ip = os.environ.get("ESP32_IP")
    if env_ip and env_ip.strip():
        return env_ip.strip()
    if IS_CLOUD:
        return "Firebase Cloud Live Sync"
    try:
        for cfg_path in (
            os.path.join(app.root_path, "config.json"),
            os.path.abspath(os.path.join(app.root_path, "..", "Rebong Sancon Funeral Service", "config.json")),
        ):
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    if cfg.get("ESP32_IP") and str(cfg["ESP32_IP"]).strip():
                        return str(cfg["ESP32_IP"]).strip()
    except Exception as e:
        print(f"[CONFIG LOAD WARNING] ESP32_IP: {e}")
    return "192.168.1.50"


# =====================================================================
# AUTHENTICATION & EXTERNAL MOBILE / ESP32 API KEY VERIFICATION
# =====================================================================
EXTERNAL_API_PATHS = {
    "/api/pos/checkout",
    "/api/order/checkout",
    "/api/order/create",
    "/api/orders/checkout",
    "/api/mobile/checkout",
    "/api/pos/scan",
    "/api/pos/qr/current",
    "/api/pos/qr/ack",
}


def _is_authorized_external_client():
    """
    Authenticate external clients (Android apps com.rebongsancon.app / com.pejas.rebongsancon & ESP32):
      1. X-API-Key header (or Authorization: ApiKey <key>) matching MOBILE_API_KEY secret.
      2. Authorization: Bearer <Firebase ID Token> verified via firebase_admin.auth.
    If MOBILE_API_KEY is unset locally (dev mode), permits requests for backwards compatibility.
    """
    expected_key = (os.environ.get("MOBILE_API_KEY") or "").strip()

    auth_header = (request.headers.get("Authorization") or "").strip()
    provided_key = (request.headers.get("X-API-Key") or "").strip()
    if not provided_key and auth_header.lower().startswith("apikey "):
        provided_key = auth_header[7:].strip()

    if expected_key:
        if provided_key and hmac.compare_digest(provided_key, expected_key):
            return True

    if auth_header.lower().startswith("bearer "):
        id_token = auth_header[7:].strip()
        if id_token:
            try:
                from firebase_admin import auth as fb_auth
                db._ensure_firebase_app()
                fb_auth.verify_id_token(id_token)
                return True
            except Exception as e:
                print(f"[AUTH WARNING] Firebase ID token verification failed: {e}")

    # Allow open fallback ONLY when no MOBILE_API_KEY secret is configured (local dev)
    if not expected_key and not IS_CLOUD:
        return True

    return False


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.path in EXTERNAL_API_PATHS or request.path.startswith("/api/mobile/"):
                if _is_authorized_external_client():
                    return f(*args, **kwargs)
                return jsonify({
                    "status": "error",
                    "message": "Unauthorized API request. Provide a valid X-API-Key or Firebase Bearer token.",
                }), 401
            if request.is_json or request.headers.get("Accept") == "application/json" or request.path.startswith("/api/"):
                return jsonify({
                    "status": "error",
                    "message": "Authentication required. Please log in or provide API credentials.",
                }), 401
            return redirect(url_for("login"))

        if session.get("must_change_password") and request.endpoint not in ("change_password", "logout", "static"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({
                    "status": "error",
                    "message": "Password change required before performing API actions.",
                }), 403
            return redirect(url_for("change_password"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"status": "error", "message": "Admin login required."}), 401
            return redirect(url_for("login"))
        if session.get("must_change_password") and request.endpoint not in ("change_password", "logout", "static"):
            return redirect(url_for("change_password"))
        if session.get("role") != "Admin":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"status": "error", "message": "Unauthorized access. Admin role required."}), 403
            return "Unauthorized access. Admin role required.", 403
        return f(*args, **kwargs)
    return decorated_function


@app.template_filter("currency")
def currency_filter(value):
    try:
        val = float(value or 0)
        return f"₱{val:,.2f}".replace(".00", "")
    except (ValueError, TypeError):
        return "₱0"


# =====================================================================
# HEALTH CHECK & IN-MEMORY QR PNG ENDPOINT
# =====================================================================
@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "ok",
        "service": "rebongsancon-management-system",
        "cloud": IS_CLOUD,
        "timezone": "Asia/Manila",
        "timestamp": db.now_pht().isoformat(),
    }), 200


@app.route("/api/qr/<casket_code>.png", methods=["GET"])
def api_qr_png(casket_code):
    """Serve crisp package QR code PNG directly from memory (zero local filesystem dependency)."""
    ok_code, clean_code, err = validators.validate_code(casket_code, "Package Code")
    if not ok_code:
        return jsonify({"status": "error", "message": err}), 400
    png_bytes = qr_generator.generate_package_qr_bytes(clean_code)
    if not png_bytes:
        return jsonify({"status": "error", "message": "Could not generate QR code."}), 500
    resp = Response(png_bytes, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# =====================================================================
# PAGE ROUTES (Python + Jinja2 HTML/CSS Templates)
# =====================================================================
@app.route("/guest")
@app.route("/showroom")
def guest_mode():
    """Client-facing Billboard & Package Promo Showroom (Guest Mode)."""
    caskets = db.get_caskets()
    billboard_setting = None
    try:
        b_doc = db.db.collection("settings").document("billboard").get()
        if b_doc.exists:
            billboard_setting = b_doc.to_dict()
    except Exception:
        pass
    billboard_image = (billboard_setting or {}).get("image_url", "/static/images/login_bg.jpg")
    return render_template("guest.html", caskets=caskets, billboard_image=billboard_image)


@app.route("/api/billboard/upload", methods=["POST"])
@admin_required
def api_upload_billboard():
    """Upload or update client billboard promo banner image to Firebase Storage (Admin only)."""
    file = request.files.get("billboard_image")
    if not file or not file.filename:
        return jsonify({"success": False, "message": "Please select an image file to upload."}), 400

    valid, _, err = validators.validate_image_file(file)
    if not valid:
        return jsonify({"success": False, "message": err}), 400

    uploaded_url = save_uploaded_image(file, folder="promos", prefix="billboard_")
    if not uploaded_url:
        return jsonify({"success": False, "message": "Failed to upload billboard image to cloud storage."}), 500

    try:
        db.db.collection("settings").document("billboard").set({
            "image_url": uploaded_url,
            "updated_at": db.now_pht(),
        }, merge=True)
    except Exception as e:
        print(f"Error saving billboard setting: {e}")
    return jsonify({
        "success": True,
        "image_url": uploaded_url,
        "message": "Billboard advertisement image updated successfully!",
    })


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        rate_key = f"login:{request.remote_addr}:{username.lower()}"
        is_limited, retry_after = validators.rate_limiter.is_limited(rate_key, max_attempts=5, window_seconds=300)
        if is_limited:
            flash(f"Too many failed attempts. Please wait {retry_after} seconds before trying again.")
            return render_template("login.html"), 429

        user = db.get_user_by_username(username)
        is_valid = False
        needs_upgrade = False
        if user:
            is_valid, needs_upgrade = validators.verify_password(user.get("password"), password)

        if user and is_valid:
            validators.rate_limiter.clear(rate_key)

            if needs_upgrade:
                try:
                    db.change_user_password(user["username"], validators.hash_password(password))
                except Exception:
                    pass

            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["must_change_password"] = bool(user.get("must_change_password", False))

            if user["role"] == "Staff":
                clock_res = db.record_staff_time_in(user["username"], user["id"])
                if clock_res.get("success"):
                    notification_service.send_attendance_email_async(user["username"], "CLOCK_IN", clock_res.get("record"))

            if session.get("must_change_password"):
                return redirect(url_for("change_password"))

            return redirect(url_for("dashboard"))

        validators.rate_limiter.record_attempt(rate_key)
        flash("Invalid username or password")
    return render_template("login.html")


@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    """Change password page (mandatory for new staff on first login, or optional anytime)."""
    if "user_id" not in session:
        return redirect(url_for("login"))

    is_mandatory = bool(session.get("must_change_password", False))

    if request.method == "POST":
        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        user = db.get_user_by_username(session.get("username"))
        is_cur_valid, _ = validators.verify_password(user.get("password"), current_password) if user else (False, False)
        if not user or not is_cur_valid:
            flash("Current password is incorrect.", "error")
            return render_template("change_password.html", is_mandatory=is_mandatory)

        ok_strength, err_strength = validators.validate_password_strength(new_password, min_length=6)
        if not ok_strength:
            flash(err_strength, "error")
            return render_template("change_password.html", is_mandatory=is_mandatory)

        if new_password == current_password:
            flash("New password must be different from your current password.", "error")
            return render_template("change_password.html", is_mandatory=is_mandatory)

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "error")
            return render_template("change_password.html", is_mandatory=is_mandatory)

        success, msg = db.change_user_password(session.get("username"), new_password)
        if success:
            session["must_change_password"] = False
            flash("Password changed successfully! You may now proceed.", "success")
            return redirect(url_for("dashboard"))
        flash(f"Failed to change password: {msg}", "error")
        return render_template("change_password.html", is_mandatory=is_mandatory)

    return render_template("change_password.html", is_mandatory=is_mandatory)


@app.route("/logout")
def logout():
    if session.get("role") == "Staff" and session.get("username"):
        clock_res = db.record_staff_time_out(session["username"])
        if clock_res.get("success"):
            notification_service.send_attendance_email_async(session["username"], "CLOCK_OUT", clock_res.get("record"))
    session.clear()
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
@admin_required
def register():
    if request.method == "POST":
        raw_u = request.form.get("username", "")
        raw_p = request.form.get("password", "")
        raw_fn = request.form.get("full_name", "")
        raw_em = request.form.get("email", "")
        raw_cn = request.form.get("contact_no", "")
        raw_r = request.form.get("role", "Staff")

        ok_u, username, err_u = validators.validate_username(raw_u)
        if not ok_u:
            flash(err_u)
            return redirect(url_for("register"))

        ok_p, err_p = validators.validate_password_strength(raw_p, min_length=6)
        if not ok_p:
            flash(err_p)
            return redirect(url_for("register"))

        ok_e, email, err_e = validators.validate_email(raw_em, allow_empty=False)
        if not ok_e:
            flash(err_e)
            return redirect(url_for("register"))

        ok_c, contact_no, err_c = validators.validate_ph_mobile(raw_cn, allow_empty=False)
        if not ok_c:
            flash(err_c)
            return redirect(url_for("register"))

        if raw_fn and str(raw_fn).strip():
            ok_fn, full_name, err_fn = validators.validate_person_name(raw_fn, "Full Name", max_length=40)
            if not ok_fn:
                flash(err_fn)
                return redirect(url_for("register"))
        else:
            full_name = username
        role = validators.validate_role(raw_r)

        _, msg = db.create_user(username, raw_p, role, full_name, email, contact_no)
        flash(msg)
        return redirect(url_for("register"))

    users = db.get_all_users()
    mail_settings = db.get_mail_settings()
    admin_emails = db.get_admin_emails()
    return render_template("register.html", page="register", users=users, mail_settings=mail_settings, admin_emails=admin_emails)


def compute_dashboard_analytics(transactions, reservations, caskets):
    """Aggregate analytics using Philippine Time (Asia/Manila)."""
    total_revenue = sum(float(t.get("total_amount", 0)) for t in transactions)
    total_collected = sum(float(t.get("down_payment", 0)) for t in transactions)
    total_receivables = sum(float(t.get("remaining_balance", 0)) for t in transactions)
    total_orders = len(transactions)

    collection_rate = round((total_collected / total_revenue * 100), 1) if total_revenue > 0 else 0.0
    avg_order_value = round(total_revenue / total_orders, 2) if total_orders > 0 else 0.0
    senior_discount_count = sum(1 for t in transactions if t.get("senior_discount_applied"))
    total_senior_discounts = sum(float(t.get("discount_amount", 0)) for t in transactions)

    now = db.now_pht()
    today_str = now.strftime("%Y-%m-%d")
    today_sales = 0.0
    for t in transactions:
        ca = t.get("created_at")
        if ca:
            try:
                date_str = db.to_pht(ca).strftime("%Y-%m-%d") if isinstance(ca, datetime) else str(ca)[:10]
                if date_str == today_str:
                    today_sales += float(t.get("down_payment", 0))
            except Exception:
                pass

    month_keys = []
    month_labels = []
    for i in range(5, -1, -1):
        y = now.year
        m = now.month - i
        while m <= 0:
            m += 12
            y -= 1
        month_keys.append(f"{y}-{m:02d}")
        month_labels.append(f"{calendar.month_abbr[m]} {y}")

    monthly_billed = {k: 0.0 for k in month_keys}
    monthly_collected = {k: 0.0 for k in month_keys}

    for t in transactions:
        ca = t.get("created_at")
        if ca:
            try:
                k = db.to_pht(ca).strftime("%Y-%m") if isinstance(ca, datetime) else str(ca)[:7]
                if k in monthly_billed:
                    monthly_billed[k] += float(t.get("total_amount", 0))
                    monthly_collected[k] += float(t.get("down_payment", 0))
            except Exception:
                pass

    monthly_billed_data = [round(monthly_billed[k], 2) for k in month_keys]
    monthly_collected_data = [round(monthly_collected[k], 2) for k in month_keys]

    package_counts = defaultdict(int)
    for t in transactions:
        cname = t.get("casket_name") or t.get("package") or "Standard Package"
        package_counts[cname] += 1

    sorted_packages = sorted(package_counts.items(), key=lambda x: x[1], reverse=True)
    package_labels = [p[0] for p in sorted_packages[:6]] if sorted_packages else ["No packages yet"]
    package_data = [p[1] for p in sorted_packages[:6]] if sorted_packages else [0]

    paid_count = 0
    partial_count = 0
    pending_count = 0
    for t in transactions:
        bal = float(t.get("remaining_balance", 0))
        dp = float(t.get("down_payment", 0))
        status = str(t.get("payment_status", ""))
        if bal <= 0 or ("Paid" in status and "Partial" not in status):
            paid_count += 1
        elif dp > 0:
            partial_count += 1
        else:
            pending_count += 1

    payment_methods = defaultdict(int)
    for t in transactions:
        pm = t.get("payment_method") or "Cash"
        payment_methods[pm] += 1

    casket_stock_labels = []
    casket_stock_data = []
    casket_stock_colors = []
    total_stock_units = 0
    for c in caskets:
        name = c.get("name", "Casket")
        short_name = name[:20] + "..." if len(name) > 20 else name
        stock = int(c.get("stock", 0))
        total_stock_units += stock
        casket_stock_labels.append(short_name)
        casket_stock_data.append(stock)
        casket_stock_colors.append("#f59e0b" if stock <= 2 else "#10b981")

    venue_counts = defaultdict(int)
    for r in reservations:
        venue = r.get("chapel_room") or "Chapel Room"
        venue_short = venue.split("(")[0].strip() if "(" in venue else venue
        venue_counts[venue_short] += 1

    return {
        "total_revenue": total_revenue,
        "total_collected": total_collected,
        "total_receivables": total_receivables,
        "collection_rate": collection_rate,
        "total_orders": total_orders,
        "avg_order_value": avg_order_value,
        "today_sales": today_sales,
        "senior_discount_count": senior_discount_count,
        "total_senior_discounts": total_senior_discounts,
        "total_stock_units": total_stock_units,
        "month_labels": month_labels,
        "monthly_billed_data": monthly_billed_data,
        "monthly_collected_data": monthly_collected_data,
        "package_labels": package_labels,
        "package_data": package_data,
        "payment_status_counts": [paid_count, partial_count, pending_count],
        "payment_methods": dict(payment_methods),
        "casket_stock_labels": casket_stock_labels,
        "casket_stock_data": casket_stock_data,
        "casket_stock_colors": casket_stock_colors,
        "venue_labels": list(venue_counts.keys()) if venue_counts else ["No reservations"],
        "venue_data": list(venue_counts.values()) if venue_counts else [0],
    }


@app.route("/")
@login_required
def home():
    return redirect("/dashboard")


@app.route("/dashboard")
@login_required
def dashboard():
    transactions = db.get_orders()
    reservations = db.get_reservations()
    caskets = db.get_caskets()

    analytics = compute_dashboard_analytics(transactions, reservations, caskets)
    today_sales = sum(float(t.get("down_payment", 0)) for t in transactions)
    low_stock_count = sum(1 for c in caskets if c.get("stock", 0) <= 2)

    if session.get("role") == "Admin":
        today_str = db.now_pht().strftime("%Y-%m-%d")
        attendance_today = db.get_all_attendance_logs(filter_date=today_str)
        staff_on_duty = sum(1 for a in attendance_today if a.get("status") == "On Duty")
        return render_template(
            "dashboard.html",
            page="dashboard",
            analytics=analytics,
            today_sales=today_sales,
            active_vigils=len(reservations),
            casket_count=len(caskets),
            low_stock_count=low_stock_count,
            reservations=reservations[:4],
            transactions=transactions[:5],
            attendance_today=attendance_today[:6],
            staff_on_duty=staff_on_duty,
        )

    today_att = db.get_today_attendance(session.get("username"))
    att_history = db.get_staff_attendance_history(session.get("username"))
    return render_template(
        "staff_dashboard.html",
        page="dashboard",
        analytics=analytics,
        today_sales=today_sales,
        casket_count=len(caskets),
        low_stock_count=low_stock_count,
        transactions=transactions[:5],
        today_attendance=today_att,
        attendance_history=att_history[:7],
    )


@app.route("/api/analytics")
@login_required
def api_analytics():
    transactions = db.get_orders()
    reservations = db.get_reservations()
    caskets = db.get_caskets()
    return jsonify(compute_dashboard_analytics(transactions, reservations, caskets))


@app.route("/attendance")
@login_required
def attendance_page():
    if session.get("role") == "Admin":
        filter_date = request.args.get("date", "").strip() or None
        search_user = request.args.get("staff", "").strip() or None
        logs = db.get_all_attendance_logs(filter_date=filter_date, search_user=search_user)
        today_str = db.now_pht().strftime("%Y-%m-%d")
        today_logs = db.get_all_attendance_logs(filter_date=today_str)
        on_duty_count = sum(1 for a in today_logs if a.get("status") == "On Duty")
        completed_count = sum(1 for a in today_logs if a.get("status") == "Completed")
        admin_emails = db.get_admin_emails()
        return render_template(
            "attendance_admin.html",
            page="attendance",
            logs=logs,
            filter_date=filter_date or "",
            search_user=search_user or "",
            on_duty_count=on_duty_count,
            completed_count=completed_count,
            total_today=len(today_logs),
            admin_emails=admin_emails,
        )

    today_att = db.get_today_attendance(session.get("username"))
    history = db.get_staff_attendance_history(session.get("username"))
    return render_template(
        "attendance_staff.html",
        page="attendance",
        today_attendance=today_att,
        attendance_history=history,
    )


@app.route("/api/attendance/clock_in", methods=["POST"])
@login_required
def api_clock_in():
    res = db.record_staff_time_in(session.get("username"), session.get("user_id"))
    if res.get("success"):
        notification_service.send_attendance_email_async(session.get("username"), "CLOCK_IN", res.get("record"))
    return jsonify(res)


@app.route("/api/attendance/clock_out", methods=["POST"])
@login_required
def api_clock_out():
    res = db.record_staff_time_out(session.get("username"))
    if res.get("success"):
        notification_service.send_attendance_email_async(session.get("username"), "CLOCK_OUT", res.get("record"))
    return jsonify(res)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    username = session.get("username")
    if request.method == "POST":
        raw_fn = request.form.get("full_name", "")
        raw_em = request.form.get("email", "")
        raw_cn = request.form.get("contact_no", "")

        ok_e, email, err_e = validators.validate_email(raw_em, allow_empty=False)
        if not ok_e:
            flash(err_e, "error")
            return redirect(url_for("profile"))

        ok_c, contact_no, err_c = validators.validate_contact_no(raw_cn, allow_empty=True)
        if not ok_c:
            flash(err_c, "error")
            return redirect(url_for("profile"))

        if raw_fn and str(raw_fn).strip():
            ok_fn, full_name, err_fn = validators.validate_person_name(raw_fn, "Full Name", max_length=40)
            if not ok_fn:
                flash(err_fn, "error")
                return redirect(url_for("profile"))
        else:
            full_name = username

        success, msg = db.update_user(username, {
            "full_name": full_name,
            "email": email,
            "contact_no": contact_no,
        })
        if success:
            flash("Profile information & email updated successfully in the system!", "success")
        else:
            flash(f"Failed to update profile: {msg}", "error")
        return redirect(url_for("profile"))

    user = db.get_user_by_username(username)
    mail_settings = db.get_mail_settings() if session.get("role") == "Admin" else None
    admin_emails = db.get_admin_emails() if session.get("role") == "Admin" else None
    return render_template("profile.html", page="profile", user=user, mail_settings=mail_settings, admin_emails=admin_emails)


@app.route("/api/users/update", methods=["POST"])
@admin_required
def api_update_user():
    data = request.get_json(silent=True) or request.form.to_dict()
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"status": "error", "message": "Username is required"}), 400

    ok_e, email, err_e = validators.validate_email(data.get("email"), allow_empty=False)
    if not ok_e:
        return jsonify({"status": "error", "message": err_e}), 400

    ok_c, contact_no, err_c = validators.validate_contact_no(data.get("contact_no"), allow_empty=True)
    if not ok_c:
        return jsonify({"status": "error", "message": err_c}), 400

    raw_fn = data.get("full_name")
    if raw_fn and str(raw_fn).strip():
        ok_fn, full_name, err_fn = validators.validate_person_name(raw_fn, "Full Name", max_length=40)
        if not ok_fn:
            return jsonify({"status": "error", "message": err_fn}), 400
    else:
        full_name = username
    role = validators.validate_role(data.get("role"))

    update_payload = {
        "full_name": full_name,
        "email": email,
        "contact_no": contact_no,
        "role": role,
    }

    new_pwd = (data.get("new_password") or "").strip()
    if new_pwd:
        ok_p, err_p = validators.validate_password_strength(new_pwd, min_length=6)
        if not ok_p:
            return jsonify({"status": "error", "message": err_p}), 400
        update_payload["password"] = validators.hash_password(new_pwd)

    success, msg = db.update_user(username, update_payload)
    if success:
        return jsonify({"status": "success", "message": f"Account '{username}' updated successfully."})
    return jsonify({"status": "error", "message": msg}), 400


@app.route("/api/users/delete", methods=["POST"])
@admin_required
def api_delete_user():
    data = request.get_json(silent=True) or request.form.to_dict()
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"status": "error", "message": "Username is required"}), 400
    if username.lower() == str(session.get("username", "")).lower():
        return jsonify({"status": "error", "message": "You cannot delete your own logged-in admin account."}), 400

    success, msg = db.delete_user(username)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 400


@app.route("/api/settings/mail", methods=["POST"])
@admin_required
def api_update_mail_settings():
    data = request.get_json(silent=True) or request.form.to_dict()
    new_settings = {
        "sender_email": data.get("sender_email"),
        "sender_password": data.get("sender_password"),
        "admin_notification_email": data.get("admin_notification_email"),
        "notifications_enabled": str(data.get("notifications_enabled", "true")).lower() in ("true", "1", "on"),
    }
    success, msg = db.save_mail_settings(new_settings)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 400


@app.route("/api/settings/test_email", methods=["POST"])
@admin_required
def api_test_email():
    data = request.get_json(silent=True) or {}
    success, msg = notification_service.send_test_email(data.get("target_email"))
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 400


@app.route("/pos")
@login_required
def pos():
    caskets = db.get_caskets()
    esp32_ip = get_esp32_ip()
    return render_template("pos.html", page="pos", caskets=caskets, esp32_ip=esp32_ip)


@app.route("/reservations")
@admin_required
def reservations_page():
    reservations = db.get_reservations()
    return render_template("reservations.html", page="reservations", reservations=reservations)


@app.route("/inventory")
@login_required
def inventory_page():
    caskets = db.get_caskets()
    return render_template("inventory.html", page="inventory", caskets=caskets, role=session.get("role"))


@app.route("/billing")
@login_required
def billing_page():
    tab = request.args.get("tab", "all")
    counts = db.get_order_counts()

    if tab == "archived":
        if session.get("role") != "Admin":
            return redirect(url_for("billing_page", tab="all"))
        orders = db.get_orders(only_archived=True)
    elif tab == "package":
        orders = db.get_orders(order_type="Package")
    elif tab == "custom":
        orders = db.get_orders(order_type="Custom")
    else:
        tab = "all"
        orders = db.get_orders(order_type=None)

    return render_template("billing.html", page="billing", transactions=orders, current_tab=tab, counts=counts, role=session.get("role"))


@app.route("/api/inventory", methods=["POST"])
@admin_required
def api_add_casket():
    data = (request.json or {}) if request.is_json else request.form.to_dict()

    image_file = request.files.get("package_image")
    if image_file and image_file.filename:
        uploaded_path = save_uploaded_image(image_file, folder="caskets")
        if uploaded_path:
            data["image_url"] = uploaded_path

    inclusions_raw = data.get("inclusions", "")
    if isinstance(inclusions_raw, str):
        data["inclusions"] = [
            validators.sanitize_text(i, max_length=200)
            for i in inclusions_raw.replace("\r", "").split("\n")
            if i.strip()
        ]

    ok_code, clean_code, err_c = validators.validate_code(data.get("casket_code"), "Package Code")
    if not ok_code:
        return jsonify({"success": False, "message": err_c}), 400
    data["casket_code"] = clean_code

    clean_name = validators.sanitize_text(data.get("name"), max_length=150)
    if not clean_name:
        return jsonify({"success": False, "message": "Package Name is required."}), 400
    data["name"] = clean_name

    ok_p, base_price, err_p = validators.validate_positive_number(data.get("base_price", 0), "Base Price", allow_zero=False)
    if not ok_p:
        return jsonify({"success": False, "message": err_p}), 400
    data["base_price"] = base_price

    ok_s, stock, err_s = validators.validate_stock_quantity(data.get("stock", 1))
    if not ok_s:
        return jsonify({"success": False, "message": err_s}), 400
    data["stock"] = stock

    success = db.add_casket(data)
    return jsonify({"success": success, "message": "Package created successfully!" if success else "Failed to create package."})


@app.route("/api/inventory/<casket_code>", methods=["PUT", "POST", "DELETE"])
@admin_required
def api_manage_casket(casket_code):
    ok_code, clean_code, err_c = validators.validate_code(casket_code, "Package Code")
    if not ok_code:
        return jsonify({"success": False, "message": err_c}), 400

    if request.method == "DELETE":
        success = db.delete_casket(clean_code)
        return jsonify({"success": success, "message": "Package deleted successfully!" if success else "Failed to delete package."})

    data = (request.json or {}) if request.is_json else request.form.to_dict()

    image_file = request.files.get("package_image")
    if image_file and image_file.filename:
        uploaded_path = save_uploaded_image(image_file, folder="caskets")
        if uploaded_path:
            data["image_url"] = uploaded_path

    inclusions_raw = data.get("inclusions", "")
    if isinstance(inclusions_raw, str) and inclusions_raw.strip():
        data["inclusions"] = [
            validators.sanitize_text(i, max_length=200)
            for i in inclusions_raw.replace("\r", "").split("\n")
            if i.strip()
        ]

    if "name" in data:
        data["name"] = validators.sanitize_text(data["name"], max_length=150)
    if "base_price" in data and data["base_price"] is not None:
        ok_p, base_price, err_p = validators.validate_positive_number(data["base_price"], "Base Price", allow_zero=False)
        if not ok_p:
            return jsonify({"success": False, "message": err_p}), 400
        data["base_price"] = base_price
    if "stock" in data and data["stock"] is not None:
        ok_s, stock, err_s = validators.validate_stock_quantity(data["stock"])
        if not ok_s:
            return jsonify({"success": False, "message": err_s}), 400
        data["stock"] = stock

    success = db.update_casket(clean_code, data)
    return jsonify({"success": success, "message": "Package updated successfully!" if success else "Failed to update package."})


@app.route("/api/inventory/<casket_code>/quick_price", methods=["POST"])
@admin_required
def api_quick_price_casket(casket_code):
    ok_code, clean_code, err_c = validators.validate_code(casket_code, "Package Code")
    if not ok_code:
        return jsonify({"success": False, "message": err_c}), 400

    data = request.json or request.form
    ok_p, base_price, err_p = validators.validate_positive_number(data.get("base_price"), "Base Price", allow_zero=False)
    if not ok_p:
        return jsonify({"success": False, "message": err_p}), 400

    ok_s, stock, err_s = validators.validate_stock_quantity(data.get("stock"))
    if not ok_s:
        return jsonify({"success": False, "message": err_s}), 400

    success, msg = db.quick_update_casket(clean_code, base_price, stock)
    if success:
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "message": msg}), 500


@app.route("/api/send_receipt", methods=["POST"])
@login_required
def send_receipt():
    data = request.json or {}
    raw_email = data.get("email")
    raw_invoice = data.get("invoice_no")

    rate_key = f"receipt:{request.remote_addr}"
    is_limited, retry_after = validators.rate_limiter.is_limited(rate_key, max_attempts=10, window_seconds=60)
    if is_limited:
        return jsonify({"status": "error", "message": f"Too many receipt requests. Please wait {retry_after} seconds."}), 429
    validators.rate_limiter.record_attempt(rate_key)

    ok_email, email, err_email = validators.validate_email(raw_email, allow_empty=False)
    if not ok_email:
        return jsonify({"status": "error", "message": err_email}), 400

    invoice_no = validators.sanitize_text(raw_invoice, max_length=50)

    try:
        mail_cfg = db.get_mail_settings()
        gmail_user = mail_cfg.get("sender_email")
        gmail_password = mail_cfg.get("sender_password")

        if not gmail_user or not gmail_password or gmail_user == "YOUR_GMAIL_ADDRESS_HERE":
            return jsonify({"status": "error", "message": "Please configure system email settings in User/Admin settings"}), 400

        order = db.get_order_by_invoice(invoice_no)
        if not order:
            return jsonify({"status": "error", "message": "Invoice not found"}), 404

        msg = MIMEMultipart("alternative")
        msg["From"] = f"RebongSancon <{gmail_user}>"
        msg["To"] = email
        msg["Subject"] = f"Official Receipt - {invoice_no}"

        html_body = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; background-color: #f8fafc; padding: 20px; }}
                .container {{ background-color: #ffffff; border: 1px solid #e2e8f0; border-top: 4px solid #eab308; border-radius: 8px; max-width: 600px; margin: 0 auto; overflow: hidden; }}
                .header {{ background-color: #000000; color: #ffffff; padding: 20px; text-align: center; }}
                .header h2 {{ margin: 0; color: #eab308; }}
                .content {{ padding: 30px; }}
                .details-table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                .details-table th, .details-table td {{ padding: 10px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
                .footer {{ background-color: #f1f5f9; padding: 15px; text-align: center; font-size: 12px; color: #64748b; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>RebongSancon Funeral Services</h2>
                    <p style="margin:5px 0 0 0; color:#cbd5e1; font-size:14px;">Official Receipt</p>
                </div>
                <div class="content">
                    <p>Dear {order.get('contact_person', 'Client')},</p>
                    <p>Thank you for trusting RebongSancon Funeral Services. Below are the details for your transaction.</p>
                    <table class="details-table">
                        <tr><th>Invoice No.</th><td><strong>{invoice_no}</strong></td></tr>
                        <tr><th>Package</th><td>{order.get('casket_name', 'Funeral Package')}</td></tr>
                        <tr><th>Total Amount</th><td>PHP {float(order.get('total_amount', 0)):,.2f}</td></tr>
                        <tr><th>Down Payment</th><td>PHP {float(order.get('down_payment', 0)):,.2f}</td></tr>
                        <tr><th>Remaining Balance</th><td><strong style="color:#b45309;">PHP {float(order.get('remaining_balance', 0)):,.2f}</strong></td></tr>
                        <tr><th>Payment Status</th><td>{order.get('payment_status', 'Pending')}</td></tr>
                    </table>
                    <p style="margin-top:30px;">If you have any questions, please contact our support.</p>
                </div>
                <div class="footer">
                    RebongSancon Management System<br>
                    Laguna University Capstone
                </div>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText("Please enable HTML to view this receipt.", "plain"))
        msg.attach(MIMEText(html_body, "html"))

        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=12)
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.send_message(msg)
        server.quit()

        return jsonify({"status": "success", "message": f"Receipt successfully sent to {email}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =====================================================================
# RESTful API ENDPOINTS (ESP32, ANDROID APPS & WEB POS)
# =====================================================================
@app.route("/api/pos/scan", methods=["GET", "POST"])
def handle_esp32_scan():
    """
    POST: Called by Mobile App, ESP32 scanner, or logged-in POS staff with {"qrCode": "CSK-OAK-01"}.
    GET:  Called by web POS to fetch the latest scanned item directly from Firestore (stateless).
    """
    if request.method == "POST":
        if "user_id" not in session and not _is_authorized_external_client():
            return jsonify({"status": "error", "message": "Unauthorized scan request."}), 401

        data = request.get_json(silent=True) or request.form
        raw_code = (
            data.get("qrCode") or data.get("qr_code") or data.get("code")
            or data.get("casket_code") or data.get("casketCode")
        )
        if raw_code:
            raw_code = str(raw_code).strip()

        ok_code, qr_code, err_code = validators.validate_code(raw_code, "QR Code")
        if not ok_code:
            return jsonify({"status": "error", "message": err_code}), 400

        item = db.get_casket_by_code(qr_code)
        now_time_12 = db.now_pht().strftime("%Y-%m-%d %I:%M:%S %p")
        if not item:
            return jsonify({
                "status": "error",
                "message": f"Package with QR code '{qr_code}' was not found in Firestore inventory.",
            }), 404

        scanned_payload = {
            "casketCode": item["casket_code"],
            "name": item["name"],
            "category": item.get("category", "Casket Package"),
            "material": item.get("material", "Hardwood"),
            "dimensions": item.get("dimensions", "Standard Full Size"),
            "basePrice": float(item["base_price"]),
            "stock": item.get("stock", 1),
            "inclusions": item.get("inclusions", []),
            "qr_image_url": item.get("qr_image_url", f"/api/qr/{item['casket_code']}.png"),
            "qr_base64": item.get("qr_base64", ""),
            "timestamp": now_time_12,
        }

        db.log_esp32_scan(qr_code, request.remote_addr or "127.0.0.1", scanned_payload)

        return jsonify({
            "status": "success",
            "message": f"QR Code {qr_code} processed & verified from Firebase Firestore",
            "scannedItem": scanned_payload,
        }), 200

    # GET: Always read directly from Firestore so multiple Cloud Run instances stay in sync
    if "user_id" not in session and not _is_authorized_external_client():
        return jsonify({"status": "error", "message": "Authentication required."}), 401

    active_item = None
    doc_data = db.get_active_scan()
    if doc_data:
        pkg = doc_data.get("package") or {}
        c_code = doc_data.get("casket_code") or doc_data.get("qr_code") or pkg.get("casketCode")
        if c_code:
            item = db.get_casket_by_code(str(c_code).strip())
            if item:
                active_item = {
                    "casketCode": item["casket_code"],
                    "name": item["name"],
                    "category": item.get("category", "Casket Package"),
                    "material": item.get("material", "Hardwood"),
                    "dimensions": item.get("dimensions", "Standard Full Size"),
                    "basePrice": float(item["base_price"]),
                    "stock": item.get("stock", 1),
                    "inclusions": item.get("inclusions", []),
                    "qr_image_url": item.get("qr_image_url", f"/api/qr/{item['casket_code']}.png"),
                    "qr_base64": item.get("qr_base64", ""),
                    "timestamp": doc_data.get("timestamp_str") or db.now_pht().strftime("%Y-%m-%d %I:%M:%S %p"),
                }

    if not active_item:
        return jsonify({"hasScan": False, "scannedItem": None}), 200

    return jsonify({"status": "success", "hasScan": True, "scannedItem": active_item}), 200


@app.route("/api/pos/scan/clear", methods=["POST"])
@login_required
def clear_esp32_scan():
    """Clear active scan document and active display QR in Firestore."""
    db.clear_active_scan()
    try:
        db.clear_active_display_qr()
    except Exception:
        pass
    return jsonify({"status": "success", "message": "Scan buffer reset."}), 200


@app.route("/api/project-qr", methods=["POST"])
@login_required
def api_project_qr():
    """
    Project selected package QR payload & metadata to ESP32 via Firebase Firestore Live Sync
    (esp32_display_qr/current). Direct LAN HTTP POST is skipped in cloud runtime.
    """
    data = request.get_json(silent=True) or request.form.to_dict()
    if not data:
        return jsonify({"status": "error", "message": "No package payload received"}), 400

    raw_code = data.get("casket_code") or data.get("casketCode") or data.get("qr_code") or data.get("qrCode")
    if not raw_code:
        return jsonify({"status": "error", "message": "Package code is required"}), 400

    item = db.get_casket_by_code(str(raw_code).strip())
    if not item:
        return jsonify({"status": "error", "message": f"Package '{raw_code}' not found in inventory"}), 404

    esp32_ip = get_esp32_ip()
    payload_to_esp32 = {
        "casket_code": item["casket_code"],
        "name": item.get("name", "Funeral Package"),
        "category": item.get("category", "Standard Casket Package"),
        "material": item.get("material", "Hardwood"),
        "dimensions": item.get("dimensions", "Standard Full Size"),
        "base_price": float(item.get("base_price", 0)),
        "stock": int(item.get("stock", 0)),
        "qr_payload": item["casket_code"],
        "qr_image_url": item.get("qr_image_url", f"/api/qr/{item['casket_code']}.png"),
        "timestamp": db.now_pht().strftime("%Y-%m-%d %I:%M:%S %p"),
    }

    firestore_synced = False
    try:
        user_name = session.get("username") or session.get("role") or "Staff"
        price_val = float(item.get("base_price", 0))
        qr_record = esp32_qr_service.generate_qr_from_payload(
            payload_text=item["casket_code"],
            title=item.get("name", "Funeral Package"),
            amount=f"PHP {price_val:,.2f}",
            subtitle="Scan with Mobile App",
            invert=True,
            uploaded_by=user_name,
            canvas_size=64,
        )
        if qr_record and qr_record.get("has_qr"):
            firestore_synced = True
    except Exception as fs_err:
        print(f"[FIREBASE QR SYNC WARNING]: {fs_err}")

    # Direct local LAN HTTP POST is only attempted during local development
    http_synced = False
    if not IS_CLOUD and esp32_ip and not esp32_ip.startswith("Firebase"):
        try:
            resp = requests.post(
                f"http://{esp32_ip}/display",
                json=payload_to_esp32,
                headers={"Content-Type": "application/json"},
                timeout=1.0,
            )
            if resp.status_code in (200, 201, 202, 204):
                http_synced = True
        except Exception:
            pass

    if firestore_synced or http_synced:
        channel_desc = (
            "Firebase Live"
            if firestore_synced and not http_synced
            else ("HTTP Gateway" if http_synced and not firestore_synced else "Firebase Live & HTTP")
        )
        return jsonify({
            "status": "success",
            "message": f"QR Code for '{item['name']}' projected to ESP32 via {channel_desc}!",
            "esp32_ip": esp32_ip,
            "firestore_synced": firestore_synced,
            "http_synced": http_synced,
            "projected_item": payload_to_esp32,
        }), 200

    return jsonify({
        "status": "error",
        "message": f"Could not project QR to ESP32 via Firebase ({esp32_ip}).",
        "esp32_ip": esp32_ip,
        "offline": True,
    }), 200


@app.route("/api/pos/qr/current", methods=["GET"])
def api_get_current_display_qr():
    """HTTPS polling endpoint for cloud-connected ESP32 QR projector displays."""
    if "user_id" not in session and not _is_authorized_external_client():
        return jsonify({"status": "error", "message": "Unauthorized QR display request."}), 401
    record = esp32_qr_service.get_active_display_qr()
    return jsonify({"status": "success", "data": record}), 200


@app.route("/api/pos/qr/clear", methods=["POST"])
@login_required
def api_clear_display_qr():
    """Reset ESP32 display back to Standby mode in Firestore."""
    res = db.clear_active_display_qr()
    return jsonify({"status": "success", "message": "ESP32 display reset to Standby mode.", "data": res}), 200


@app.route("/api/pos/qr/ack", methods=["POST"])
def api_ack_display_qr():
    """Acknowledge hardware display confirmation from ESP32."""
    if "user_id" not in session and not _is_authorized_external_client():
        return jsonify({"status": "error", "message": "Unauthorized hardware ack."}), 401
    data = request.get_json(silent=True) or {}
    qr_id = data.get("qr_id")
    ip_addr = data.get("client_ip") or request.remote_addr or "127.0.0.1"
    device_name = data.get("device", "ESP32")
    db.ack_hardware_qr_displayed(qr_id, ip_addr, device_name=device_name)
    return jsonify({"status": "success", "message": "Hardware display confirmed."}), 200


@app.route("/api/esp32/config", methods=["GET"])
@login_required
def api_esp32_config():
    esp32_ip = get_esp32_ip()
    return jsonify({
        "status": "success",
        "esp32_ip": esp32_ip,
        "cloud_mode": IS_CLOUD,
        "display_endpoint": "/api/pos/qr/current" if IS_CLOUD else f"http://{esp32_ip}/display",
    }), 200


_recent_checkout_signatures = {}


@app.route("/api/pos/checkout", methods=["POST"])
@app.route("/api/order/checkout", methods=["POST"])
@app.route("/api/order/create", methods=["POST"])
@app.route("/api/orders/checkout", methods=["POST"])
@app.route("/api/mobile/checkout", methods=["POST"])
@login_required
def process_checkout():
    """Validate and record POS / Mobile App order into Firestore orders & reservations."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    if not payload:
        return jsonify({"status": "error", "message": "No payload provided"}), 400

    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    pricing = payload.get("pricing") if isinstance(payload.get("pricing"), dict) else {}

    raw_deceased = (
        customer.get("deceasedName") or customer.get("deceased_name")
        or payload.get("deceased_name") or payload.get("deceasedName")
        or payload.get("deceased") or ""
    )
    raw_contact_person = (
        customer.get("contactPerson") or customer.get("contact_person")
        or payload.get("contact_person") or payload.get("contactPerson")
        or payload.get("client_name") or payload.get("clientName") or ""
    )
    raw_contact_number = (
        customer.get("contactNumber") or customer.get("contact_number")
        or payload.get("contact_number") or payload.get("contactNumber")
        or payload.get("mobile") or payload.get("phone") or ""
    )

    ok_dec, clean_deceased, err_dec = validators.validate_person_name(raw_deceased, "Deceased Full Name", max_length=40)
    if not ok_dec:
        return jsonify({"status": "error", "message": err_dec}), 400

    ok_cp, clean_contact_person, err_cp = validators.validate_person_name(raw_contact_person, "Contact Person / Client Name", max_length=40)
    if not ok_cp:
        return jsonify({"status": "error", "message": err_cp}), 400

    ok_c, final_contact_no, err_c = validators.validate_ph_mobile(raw_contact_number, allow_empty=False)
    if not ok_c:
        return jsonify({"status": "error", "message": err_c}), 400

    allowed_methods = {"Cash", "GCash", "Bank Transfer", "Installment"}
    payment_method = str(pricing.get("paymentMethod") or payload.get("payment_method") or payload.get("paymentMethod") or "Cash").strip()
    if payment_method not in allowed_methods:
        return jsonify({"status": "error", "message": f"Invalid payment method '{payment_method}'."}), 400
    pricing["paymentMethod"] = payment_method

    raw_total = pricing.get("totalAmount") if "totalAmount" in pricing else (payload.get("total_amount") or payload.get("totalAmount") or payload.get("total"))
    raw_down = pricing.get("downPayment") if "downPayment" in pricing else (payload.get("down_payment") or payload.get("downPayment") or payload.get("down") or 0)

    ok_tot, total_amt, err_tot = validators.validate_positive_number(raw_total, "Total Amount", allow_zero=False)
    if not ok_tot:
        return jsonify({"status": "error", "message": err_tot or "Total Amount must be greater than ₱0."}), 400

    ok_down, down_pmt, err_down = validators.validate_positive_number(raw_down, "Down Payment", allow_zero=True)
    if not ok_down:
        return jsonify({"status": "error", "message": err_down}), 400

    if down_pmt > total_amt + 0.01:
        return jsonify({
            "status": "error",
            "message": f"Down Payment (₱{down_pmt:,.2f}) cannot exceed Total Amount (₱{total_amt:,.2f}). Please check for typing errors.",
        }), 400

    rem_bal = round(max(0.0, total_amt - down_pmt), 2)

    order_type = payload.get("orderType") or payload.get("order_type") or "Package"
    if order_type == "Package":
        pkg = payload.get("scannedPackage") if isinstance(payload.get("scannedPackage"), dict) else {}
        pkg_code = str(
            pkg.get("code") or pkg.get("casketCode")
            or payload.get("casket_code") or payload.get("casketCode")
            or payload.get("package_code") or payload.get("code") or ""
        ).strip()

        if not pkg_code:
            active_doc = db.get_active_scan()
            if active_doc:
                pkg_code = active_doc.get("casket_code") or active_doc.get("qr_code") or ""

        if not pkg_code:
            return jsonify({"status": "error", "message": "Please select or scan a funeral package before checking out."}), 400

        inv_item = db.get_casket_by_code(pkg_code)
        if not inv_item:
            return jsonify({"status": "error", "message": f"Selected package '{pkg_code}' was not found in inventory."}), 404
        if int(inv_item.get("stock", 0)) <= 0:
            return jsonify({"status": "error", "message": f"Package '{inv_item.get('name', pkg_code)}' is currently Out of Stock."}), 400

        official_base_price = float(inv_item.get("base_price", 0))
        addons_list = payload.get("addOns") or payload.get("addons") or []
        addons_total = sum(float(a.get("price", a.get("addon_price", 0))) for a in addons_list if isinstance(a, dict))
        subtotal = official_base_price + addons_total

        discount_pct = float(pricing.get("discountPercent") or payload.get("discount_percent") or 0.0)
        if discount_pct < 0 or discount_pct > 100:
            return jsonify({"status": "error", "message": "Invalid discount percentage."}), 400
        if discount_pct > 20 and session.get("role") != "Admin":
            discount_pct = 20.0

        discount_amt = round((subtotal * discount_pct) / 100.0, 2)
        expected_total = round(max(0.0, subtotal - discount_amt), 2)

        if abs(total_amt - expected_total) > 1.0:
            total_amt = expected_total
            if down_pmt > total_amt + 0.01:
                down_pmt = total_amt
            rem_bal = round(max(0.0, total_amt - down_pmt), 2)

    elif order_type == "Custom":
        custom_details = payload.get("customDetails") or payload.get("custom_details") or {}
        casket_price = float(custom_details.get("casket", {}).get("price", 0) if isinstance(custom_details.get("casket"), dict) else 0)
        chapel_price = float(custom_details.get("chapel", {}).get("price", 0) if isinstance(custom_details.get("chapel"), dict) else 0)
        flower_price = float(custom_details.get("flower", {}).get("price", 0) if isinstance(custom_details.get("flower"), dict) else 0)
        hearse_price = float(custom_details.get("hearse", {}).get("price", 0) if isinstance(custom_details.get("hearse"), dict) else 0)
        embalming_price = float(custom_details.get("embalming", {}).get("price", 0) if isinstance(custom_details.get("embalming"), dict) else 0)
        addons_list = payload.get("addOns") or payload.get("addons") or []
        addons_total = sum(float(a.get("price", a.get("addon_price", 0))) for a in addons_list if isinstance(a, dict))
        computed_subtotal = casket_price + chapel_price + flower_price + hearse_price + embalming_price + addons_total
        if computed_subtotal > 0:
            discount_pct = float(pricing.get("discountPercent") or payload.get("discount_percent") or 0.0)
            discount_pct = min(100.0, max(0.0, discount_pct))
            expected_total = round(max(0.0, computed_subtotal - (computed_subtotal * discount_pct / 100.0)), 2)
            if abs(total_amt - expected_total) > 1.0:
                total_amt = expected_total
                if down_pmt > total_amt + 0.01:
                    down_pmt = total_amt
                rem_bal = round(max(0.0, total_amt - down_pmt), 2)

    now_ts = time.time()
    for sig_key in list(_recent_checkout_signatures.keys()):
        if now_ts - _recent_checkout_signatures[sig_key] > 1.5:
            _recent_checkout_signatures.pop(sig_key, None)

    order_sig = (clean_deceased.lower(), final_contact_no, round(total_amt, 2), order_type)
    if order_sig in _recent_checkout_signatures:
        return jsonify({
            "status": "error",
            "message": "A duplicate order with the exact same client and amount was just recorded. Please check Billing & Invoices before submitting again.",
        }), 409

    customer["deceasedName"] = clean_deceased
    customer["contactPerson"] = clean_contact_person
    customer["contactNumber"] = final_contact_no
    payload["customer"] = customer
    payload["deceased_name"] = clean_deceased
    payload["contact_person"] = clean_contact_person
    payload["contact_number"] = final_contact_no

    pricing["totalAmount"] = total_amt
    pricing["downPayment"] = down_pmt
    pricing["remainingBalance"] = rem_bal
    payload["pricing"] = pricing
    payload["total_amount"] = total_amt
    payload["down_payment"] = down_pmt
    payload["remaining_balance"] = rem_bal

    try:
        invoice_no = db.create_order(payload)
        _recent_checkout_signatures[order_sig] = now_ts
        db.clear_active_scan()
        return jsonify({
            "status": "success",
            "message": "Order recorded successfully in Firestore (Billing & Reservations)",
            "invoiceNo": invoice_no,
        }), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/payment/update", methods=["POST"])
@login_required
def api_update_payment():
    data = request.json or {}
    invoice_no = validators.sanitize_text(data.get("invoice_no"), max_length=50)

    ok_amt, add_payment, err_amt = validators.validate_positive_number(data.get("add_payment"), "Payment amount", allow_zero=False)
    if not invoice_no or not ok_amt:
        return jsonify({"status": "error", "message": err_amt or "Invalid payment amount"}), 400

    order = db.get_order_by_invoice(invoice_no)
    if not order:
        return jsonify({"status": "error", "message": "Invoice not found"}), 404

    rem_balance = float(order.get("remaining_balance", 0))
    if rem_balance <= 0:
        return jsonify({
            "status": "error",
            "message": f"Invoice {invoice_no} is already fully settled (remaining balance is ₱0.00). No additional payment is required.",
        }), 400

    if add_payment > rem_balance + 0.01:
        return jsonify({
            "status": "error",
            "message": f"Payment amount (₱{add_payment:,.2f}) cannot exceed remaining balance (₱{rem_balance:,.2f}).",
        }), 400

    success, msg = db.update_payment(invoice_no, add_payment)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500


@app.route("/api/order/discount", methods=["POST"])
@admin_required
def api_apply_order_discount():
    data = request.get_json(silent=True) or {}
    invoice_no = validators.sanitize_text(data.get("invoice_no"), max_length=60)
    if not invoice_no:
        return jsonify({"status": "error", "message": "Invoice number is required."}), 400

    discount_type = str(data.get("discount_type") or "percent").strip().lower()
    if discount_type not in ("percent", "fixed"):
        return jsonify({"status": "error", "message": "Discount type must be 'percent' or 'fixed'."}), 400

    ok_val, discount_val, err_val = validators.validate_positive_number(data.get("discount_value"), "Discount value", allow_zero=True)
    if not ok_val:
        return jsonify({"status": "error", "message": err_val}), 400
    if discount_type == "percent" and discount_val > 100:
        return jsonify({"status": "error", "message": "Discount percentage cannot exceed 100%."}), 400

    reason = validators.sanitize_text(data.get("reason"), max_length=200)
    applied_by = session.get("username") or "Admin"

    success, msg, updated = db.apply_order_discount(
        invoice_no=invoice_no,
        discount_type=discount_type,
        discount_value=discount_val,
        reason=reason,
        applied_by=applied_by,
    )
    if success:
        return jsonify({"status": "success", "message": msg, "data": updated}), 200
    status_code = 404 if "not found" in msg.lower() else 400
    return jsonify({"status": "error", "message": msg}), status_code


@app.route("/api/reservation/venue", methods=["POST"])
@admin_required
def api_update_venue():
    data = request.json or {}
    res_code = validators.sanitize_text(data.get("reservation_code"), max_length=50)
    raw_venue = data.get("chapel_room")
    if not res_code or not raw_venue:
        return jsonify({"status": "error", "message": "Missing required fields"}), 400

    venue = validators.sanitize_text(raw_venue, max_length=100)
    success, msg = db.update_reservation_venue(res_code, venue)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500


@app.route("/api/order/<invoice_no>", methods=["GET"])
@login_required
def api_get_order_details(invoice_no):
    order = db.get_order_by_invoice(invoice_no)
    if not order:
        return jsonify({"status": "error", "message": "Order not found"}), 404
    return jsonify({"status": "success", "order": order})


@app.route("/api/order/complete", methods=["POST"])
@login_required
def api_complete_order():
    data = request.json or {}
    invoice_no = data.get("invoice_no")
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.update_order_status(invoice_no, "Completed")
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500


@app.route("/api/order/archive", methods=["POST"])
@admin_required
def api_archive_order():
    data = request.json or {}
    invoice_no = data.get("invoice_no")
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.archive_order(invoice_no)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500


@app.route("/api/order/restore", methods=["POST"])
@admin_required
def api_restore_order():
    data = request.json or {}
    invoice_no = data.get("invoice_no")
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.restore_order(invoice_no)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500


@app.route("/api/order/delete_permanently", methods=["POST"])
@admin_required
def api_delete_order_permanently():
    data = request.json or {}
    invoice_no = data.get("invoice_no")
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.delete_order_permanently(invoice_no)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500


if __name__ == "__main__":
    print("=" * 65)
    print("RebongSancon Management System (Firebase Cloud-Ready Flask)")
    print("Server running locally at: http://127.0.0.1:5000")
    print("=" * 65)
    app.run(host="0.0.0.0", port=5000, debug=True)
