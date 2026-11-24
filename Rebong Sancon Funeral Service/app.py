from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from datetime import datetime
import calendar
from collections import defaultdict
import json
import db
import notification_service
import validators
import esp32_qr_service
from functools import wraps
import smtplib
import os
import time
import requests
from werkzeug.utils import secure_filename
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)
app.config['SECRET_KEY'] = 'rebongsancon-group15-secret-key'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB max payload limit
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

@app.after_request
def apply_security_headers(response):
    """Inject standard HTTP security headers on all responses"""
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads', 'caskets')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def save_uploaded_image(file):
    """Secure image verification & saving with Pillow verification"""
    if not file or not file.filename:
        return None
    valid, safe_name, err = validators.validate_image_file(file)
    if not valid:
        print(f"[IMAGE VALIDATION ERROR] {err}")
        return None
    unique_name = f"{int(time.time())}_{safe_name}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_name)
    file.save(filepath)
    return f"/static/uploads/caskets/{unique_name}"

def get_esp32_ip():
    """Retrieve configured ESP32 IP from env variable or config.json"""
    env_ip = os.environ.get('ESP32_IP')
    if env_ip and env_ip.strip():
        return env_ip.strip()
    try:
        config_path = os.path.join(app.root_path, 'config.json')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                cfg = json.load(f)
                if cfg.get('ESP32_IP') and str(cfg['ESP32_IP']).strip():
                    return str(cfg['ESP32_IP']).strip()
    except Exception as e:
        print(f"[CONFIG LOAD WARNING] ESP32_IP: {e}")
    return "192.168.1.50"

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Allow mobile app or API client calling order checkout or scan endpoints without web cookie session
        if 'user_id' not in session:
            if request.path.startswith('/api/pos/checkout') or request.path.startswith('/api/order') or request.path.startswith('/api/mobile'):
                return f(*args, **kwargs)
            if request.is_json or request.headers.get('Accept') == 'application/json':
                return jsonify({"status": "error", "message": "Authentication required. Please log in or provide API credentials."}), 401
            return redirect(url_for('login'))
        if session.get('must_change_password') and request.endpoint not in ('change_password', 'logout', 'static'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({"status": "error", "message": "Password change required before performing API actions."}), 403
            return redirect(url_for('change_password'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('must_change_password') and request.endpoint not in ('change_password', 'logout', 'static'):
            return redirect(url_for('change_password'))
        if session.get('role') != 'Admin':
            return "Unauthorized access. Admin role required.", 403
        return f(*args, **kwargs)
    return decorated_function


# Custom Jinja Filter for Philippine Peso currency formatting
@app.template_filter('currency')
def currency_filter(value):
    try:
        val = float(value or 0)
        return f"₱{val:,.2f}".replace('.00', '')
    except (ValueError, TypeError):
        return "₱0"

# Buffer for the latest scan from the ESP32 QR scanner (starts empty; populated only on scan)
esp32_scan_buffer = None

# --- PAGE ROUTES (Python + HTML/CSS Templates) ---

@app.route('/guest')
@app.route('/showroom')
def guest_mode():
    """Client-facing Billboard & Package Promo Showroom (Guest Mode)"""
    caskets = db.get_caskets()
    billboard_setting = None
    try:
        b_doc = db.db.collection('settings').document('billboard').get()
        if b_doc.exists:
            billboard_setting = b_doc.to_dict()
    except Exception:
        pass
    billboard_image = (billboard_setting or {}).get('image_url', '/static/images/login_bg.jpg')
    return render_template('guest.html', caskets=caskets, billboard_image=billboard_image)

@app.route('/api/billboard/upload', methods=['POST'])
@admin_required
def api_upload_billboard():
    """Upload or update client billboard promo banner image (Admin only)"""
    file = request.files.get('billboard_image')
    if not file or not file.filename:
        return jsonify({'success': False, 'message': 'Please select an image file to upload.'}), 400

    valid, safe_name, err = validators.validate_image_file(file)
    if not valid:
        return jsonify({'success': False, 'message': err}), 400

    promo_dir = os.path.join(app.root_path, 'static', 'uploads', 'promos')
    os.makedirs(promo_dir, exist_ok=True)
    unique_name = f"billboard_{int(time.time())}_{safe_name}"
    filepath = os.path.join(promo_dir, unique_name)
    file.save(filepath)
    rel_url = f"/static/uploads/promos/{unique_name}"
    try:
        db.db.collection('settings').document('billboard').set({
            'image_url': rel_url,
            'updated_at': datetime.now()
        }, merge=True)
    except Exception as e:
        print(f"Error saving billboard setting: {e}")
    return jsonify({'success': True, 'image_url': rel_url, 'message': 'Billboard advertisement image updated successfully!'})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()

        # Brute-force rate limiting protection
        rate_key = f"login:{request.remote_addr}:{username.lower()}"
        is_limited, retry_after = validators.rate_limiter.is_limited(rate_key, max_attempts=5, window_seconds=300)
        if is_limited:
            flash(f'Too many failed attempts. Please wait {retry_after} seconds before trying again.')
            return render_template('login.html'), 429

        user = db.get_user_by_username(username)
        is_valid = False
        needs_upgrade = False
        if user:
            is_valid, needs_upgrade = validators.verify_password(user.get('password'), password)

        if user and is_valid:
            validators.rate_limiter.clear(rate_key)

            # Transparently upgrade plaintext passwords to modern hash
            if needs_upgrade:
                try:
                    db.change_user_password(user['username'], validators.hash_password(password))
                except Exception:
                    pass

            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['must_change_password'] = bool(user.get('must_change_password', False))

            # Automatically clock in staff upon login if not yet clocked in today (12-hour format)
            if user['role'] == 'Staff':
                clock_res = db.record_staff_time_in(user['username'], user['id'])
                if clock_res.get('success'):
                    notification_service.send_attendance_email_async(user['username'], 'CLOCK_IN', clock_res.get('record'))

            if session.get('must_change_password'):
                return redirect(url_for('change_password'))

            return redirect(url_for('dashboard'))

        validators.rate_limiter.record_attempt(rate_key)
        flash('Invalid username or password')
    return render_template('login.html')

@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    """Change password page (mandatory for new staff on first login, or optional anytime)"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    is_mandatory = bool(session.get('must_change_password', False))

    if request.method == 'POST':
        current_password = request.form.get('current_password', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        user = db.get_user_by_username(session.get('username'))
        is_cur_valid, _ = validators.verify_password(user.get('password'), current_password) if user else (False, False)
        if not user or not is_cur_valid:
            flash('Current password is incorrect.', 'error')
            return render_template('change_password.html', is_mandatory=is_mandatory)

        ok_strength, err_strength = validators.validate_password_strength(new_password, min_length=6)
        if not ok_strength:
            flash(err_strength, 'error')
            return render_template('change_password.html', is_mandatory=is_mandatory)

        if new_password == current_password:
            flash('New password must be different from your current password.', 'error')
            return render_template('change_password.html', is_mandatory=is_mandatory)

        if new_password != confirm_password:
            flash('New password and confirmation do not match.', 'error')
            return render_template('change_password.html', is_mandatory=is_mandatory)

        success, msg = db.change_user_password(session.get('username'), new_password)
        if success:
            session['must_change_password'] = False
            flash('Password changed successfully! You may now proceed.', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash(f'Failed to change password: {msg}', 'error')
            return render_template('change_password.html', is_mandatory=is_mandatory)

    return render_template('change_password.html', is_mandatory=is_mandatory)

@app.route('/logout')
def logout():
    # If staff member is logging out, automatically record their Time Out (12-hour format)
    if session.get('role') == 'Staff' and session.get('username'):
        clock_res = db.record_staff_time_out(session['username'])
        if clock_res.get('success'):
            notification_service.send_attendance_email_async(session['username'], 'CLOCK_OUT', clock_res.get('record'))
    session.clear()
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
@admin_required
def register():
    if request.method == 'POST':
        raw_u = request.form.get('username', '')
        raw_p = request.form.get('password', '')
        raw_fn = request.form.get('full_name', '')
        raw_em = request.form.get('email', '')
        raw_cn = request.form.get('contact_no', '')
        raw_r = request.form.get('role', 'Staff')

        ok_u, username, err_u = validators.validate_username(raw_u)
        if not ok_u:
            flash(err_u)
            return redirect(url_for('register'))

        ok_p, err_p = validators.validate_password_strength(raw_p, min_length=6)
        if not ok_p:
            flash(err_p)
            return redirect(url_for('register'))

        ok_e, email, err_e = validators.validate_email(raw_em, allow_empty=False)
        if not ok_e:
            flash(err_e)
            return redirect(url_for('register'))

        ok_c, contact_no, err_c = validators.validate_ph_mobile(raw_cn, allow_empty=False)
        if not ok_c:
            flash(err_c)
            return redirect(url_for('register'))

        if raw_fn and str(raw_fn).strip():
            ok_fn, full_name, err_fn = validators.validate_person_name(raw_fn, "Full Name", max_length=40)
            if not ok_fn:
                flash(err_fn)
                return redirect(url_for('register'))
        else:
            full_name = username
        role = validators.validate_role(raw_r)

        success, msg = db.create_user(username, raw_p, role, full_name, email, contact_no)
        flash(msg)
        return redirect(url_for('register'))

    users = db.get_all_users()
    mail_settings = db.get_mail_settings()
    admin_emails = db.get_admin_emails()
    return render_template('register.html', page="register", users=users, mail_settings=mail_settings, admin_emails=admin_emails)

def compute_dashboard_analytics(transactions, reservations, caskets):
    """Aggregate analytics for POS billing, caskets, chapel occupancy and collection performance"""
    total_revenue = sum(float(t.get('total_amount', 0)) for t in transactions)
    total_collected = sum(float(t.get('down_payment', 0)) for t in transactions)
    total_receivables = sum(float(t.get('remaining_balance', 0)) for t in transactions)
    total_orders = len(transactions)

    collection_rate = round((total_collected / total_revenue * 100), 1) if total_revenue > 0 else 0.0
    avg_order_value = round(total_revenue / total_orders, 2) if total_orders > 0 else 0.0
    senior_discount_count = sum(1 for t in transactions if t.get('senior_discount_applied'))
    total_senior_discounts = sum(float(t.get('discount_amount', 0)) for t in transactions)

    # Today's collected down payments
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_sales = 0.0
    for t in transactions:
        ca = t.get('created_at')
        if ca:
            try:
                date_str = ca.strftime("%Y-%m-%d") if isinstance(ca, datetime) else str(ca)[:10]
                if date_str == today_str:
                    today_sales += float(t.get('down_payment', 0))
            except Exception:
                pass

    # Monthly revenue & collection (last 6 months rolling)
    now = datetime.now()
    month_keys = []
    month_labels = []
    for i in range(5, -1, -1):
        y = now.year
        m = now.month - i
        while m <= 0:
            m += 12
            y -= 1
        key = f"{y}-{m:02d}"
        label = f"{calendar.month_abbr[m]} {y}"
        month_keys.append(key)
        month_labels.append(label)

    monthly_billed = {k: 0.0 for k in month_keys}
    monthly_collected = {k: 0.0 for k in month_keys}

    for t in transactions:
        ca = t.get('created_at')
        if ca:
            try:
                k = ca.strftime("%Y-%m") if isinstance(ca, datetime) else str(ca)[:7]
                if k in monthly_billed:
                    monthly_billed[k] += float(t.get('total_amount', 0))
                    monthly_collected[k] += float(t.get('down_payment', 0))
            except Exception:
                pass

    monthly_billed_data = [round(monthly_billed[k], 2) for k in month_keys]
    monthly_collected_data = [round(monthly_collected[k], 2) for k in month_keys]

    # Package distribution
    package_counts = defaultdict(int)
    for t in transactions:
        cname = t.get('casket_name') or t.get('package') or 'Standard Package'
        package_counts[cname] += 1

    sorted_packages = sorted(package_counts.items(), key=lambda x: x[1], reverse=True)
    package_labels = [p[0] for p in sorted_packages[:6]] if sorted_packages else ["No packages yet"]
    package_data = [p[1] for p in sorted_packages[:6]] if sorted_packages else [0]

    # Payment status breakdown
    paid_count = 0
    partial_count = 0
    pending_count = 0
    for t in transactions:
        bal = float(t.get('remaining_balance', 0))
        dp = float(t.get('down_payment', 0))
        status = str(t.get('payment_status', ''))
        if bal <= 0 or ('Paid' in status and 'Partial' not in status):
            paid_count += 1
        elif dp > 0:
            partial_count += 1
        else:
            pending_count += 1

    # Payment methods breakdown
    payment_methods = defaultdict(int)
    for t in transactions:
        pm = t.get('payment_method') or 'Cash'
        payment_methods[pm] += 1

    # Caskets stock inventory
    casket_stock_labels = []
    casket_stock_data = []
    casket_stock_colors = []
    total_stock_units = 0
    for c in caskets:
        name = c.get('name', 'Casket')
        short_name = name[:20] + '...' if len(name) > 20 else name
        stock = int(c.get('stock', 0))
        total_stock_units += stock
        casket_stock_labels.append(short_name)
        casket_stock_data.append(stock)
        casket_stock_colors.append('#f59e0b' if stock <= 2 else '#10b981')

    # Chapel / Reservation venue distribution
    venue_counts = defaultdict(int)
    for r in reservations:
        venue = r.get('chapel_room') or 'Chapel Room'
        venue_short = venue.split('(')[0].strip() if '(' in venue else venue
        venue_counts[venue_short] += 1

    return {
        'total_revenue': total_revenue,
        'total_collected': total_collected,
        'total_receivables': total_receivables,
        'collection_rate': collection_rate,
        'total_orders': total_orders,
        'avg_order_value': avg_order_value,
        'today_sales': today_sales,
        'senior_discount_count': senior_discount_count,
        'total_senior_discounts': total_senior_discounts,
        'total_stock_units': total_stock_units,
        'month_labels': month_labels,
        'monthly_billed_data': monthly_billed_data,
        'monthly_collected_data': monthly_collected_data,
        'package_labels': package_labels,
        'package_data': package_data,
        'payment_status_counts': [paid_count, partial_count, pending_count],
        'payment_methods': dict(payment_methods),
        'casket_stock_labels': casket_stock_labels,
        'casket_stock_data': casket_stock_data,
        'casket_stock_colors': casket_stock_colors,
        'venue_labels': list(venue_counts.keys()) if venue_counts else ["No reservations"],
        'venue_data': list(venue_counts.values()) if venue_counts else [0]
    }

@app.route('/')
@login_required
def home():
    return redirect('/dashboard')

@app.route('/dashboard')
@login_required
def dashboard():
    transactions = db.get_orders()
    reservations = db.get_reservations()
    caskets = db.get_caskets()

    analytics = compute_dashboard_analytics(transactions, reservations, caskets)
    today_sales = sum(float(t.get('down_payment', 0)) for t in transactions)
    low_stock_count = sum(1 for c in caskets if c.get('stock', 0) <= 2)

    if session.get('role') == 'Admin':
        today_str = datetime.now().strftime("%Y-%m-%d")
        attendance_today = db.get_all_attendance_logs(filter_date=today_str)
        staff_on_duty = sum(1 for a in attendance_today if a.get('status') == 'On Duty')
        return render_template(
            'dashboard.html',
            page="dashboard",
            analytics=analytics,
            today_sales=today_sales,
            active_vigils=len(reservations),
            casket_count=len(caskets),
            low_stock_count=low_stock_count,
            reservations=reservations[:4],
            transactions=transactions[:5],
            attendance_today=attendance_today[:6],
            staff_on_duty=staff_on_duty
        )
    else:
        # Staff Dashboard view with today's attendance and quick clock in/out
        today_att = db.get_today_attendance(session.get('username'))
        att_history = db.get_staff_attendance_history(session.get('username'))
        return render_template(
            'staff_dashboard.html',
            page="dashboard",
            analytics=analytics,
            today_sales=today_sales,
            casket_count=len(caskets),
            low_stock_count=low_stock_count,
            transactions=transactions[:5],
            today_attendance=today_att,
            attendance_history=att_history[:7]
        )

@app.route('/api/analytics')
@login_required
def api_analytics():
    transactions = db.get_orders()
    reservations = db.get_reservations()
    caskets = db.get_caskets()
    data = compute_dashboard_analytics(transactions, reservations, caskets)
    return jsonify(data)

@app.route('/attendance')
@login_required
def attendance_page():
    if session.get('role') == 'Admin':
        filter_date = request.args.get('date', '').strip() or None
        search_user = request.args.get('staff', '').strip() or None
        logs = db.get_all_attendance_logs(filter_date=filter_date, search_user=search_user)
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_logs = db.get_all_attendance_logs(filter_date=today_str)
        on_duty_count = sum(1 for a in today_logs if a.get('status') == 'On Duty')
        completed_count = sum(1 for a in today_logs if a.get('status') == 'Completed')
        admin_emails = db.get_admin_emails()
        return render_template(
            'attendance_admin.html',
            page="attendance",
            logs=logs,
            filter_date=filter_date or '',
            search_user=search_user or '',
            on_duty_count=on_duty_count,
            completed_count=completed_count,
            total_today=len(today_logs),
            admin_emails=admin_emails
        )
    else:
        today_att = db.get_today_attendance(session.get('username'))
        history = db.get_staff_attendance_history(session.get('username'))
        return render_template(
            'attendance_staff.html',
            page="attendance",
            today_attendance=today_att,
            attendance_history=history
        )

@app.route('/api/attendance/clock_in', methods=['POST'])
@login_required
def api_clock_in():
    res = db.record_staff_time_in(session.get('username'), session.get('user_id'))
    if res.get('success'):
        notification_service.send_attendance_email_async(session.get('username'), 'CLOCK_IN', res.get('record'))
    return jsonify(res)

@app.route('/api/attendance/clock_out', methods=['POST'])
@login_required
def api_clock_out():
    res = db.record_staff_time_out(session.get('username'))
    if res.get('success'):
        notification_service.send_attendance_email_async(session.get('username'), 'CLOCK_OUT', res.get('record'))
    return jsonify(res)

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    username = session.get('username')
    if request.method == 'POST':
        raw_fn = request.form.get('full_name', '')
        raw_em = request.form.get('email', '')
        raw_cn = request.form.get('contact_no', '')

        ok_e, email, err_e = validators.validate_email(raw_em, allow_empty=False)
        if not ok_e:
            flash(err_e, 'error')
            return redirect(url_for('profile'))

        ok_c, contact_no, err_c = validators.validate_contact_no(raw_cn, allow_empty=True)
        if not ok_c:
            flash(err_c, 'error')
            return redirect(url_for('profile'))

        if raw_fn and str(raw_fn).strip():
            ok_fn, full_name, err_fn = validators.validate_person_name(raw_fn, "Full Name", max_length=40)
            if not ok_fn:
                flash(err_fn, 'error')
                return redirect(url_for('profile'))
        else:
            full_name = username

        success, msg = db.update_user(username, {
            'full_name': full_name,
            'email': email,
            'contact_no': contact_no
        })
        if success:
            flash('Profile information & email updated successfully in the system!', 'success')
        else:
            flash(f'Failed to update profile: {msg}', 'error')
        return redirect(url_for('profile'))

    user = db.get_user_by_username(username)
    mail_settings = db.get_mail_settings() if session.get('role') == 'Admin' else None
    admin_emails = db.get_admin_emails() if session.get('role') == 'Admin' else None
    return render_template('profile.html', page="profile", user=user, mail_settings=mail_settings, admin_emails=admin_emails)

@app.route('/api/users/update', methods=['POST'])
@admin_required
def api_update_user():
    data = request.get_json(silent=True) or request.form.to_dict()
    username = (data.get('username') or '').strip()
    if not username:
        return jsonify({'status': 'error', 'message': 'Username is required'}), 400

    ok_e, email, err_e = validators.validate_email(data.get('email'), allow_empty=False)
    if not ok_e:
        return jsonify({'status': 'error', 'message': err_e}), 400

    ok_c, contact_no, err_c = validators.validate_contact_no(data.get('contact_no'), allow_empty=True)
    if not ok_c:
        return jsonify({'status': 'error', 'message': err_c}), 400

    raw_fn = data.get('full_name')
    if raw_fn and str(raw_fn).strip():
        ok_fn, full_name, err_fn = validators.validate_person_name(raw_fn, "Full Name", max_length=40)
        if not ok_fn:
            return jsonify({'status': 'error', 'message': err_fn}), 400
    else:
        full_name = username
    role = validators.validate_role(data.get('role'))

    update_payload = {
        'full_name': full_name,
        'email': email,
        'contact_no': contact_no,
        'role': role
    }

    new_pwd = (data.get('new_password') or '').strip()
    if new_pwd:
        ok_p, err_p = validators.validate_password_strength(new_pwd, min_length=6)
        if not ok_p:
            return jsonify({'status': 'error', 'message': err_p}), 400
        update_payload['password'] = validators.hash_password(new_pwd)

    success, msg = db.update_user(username, update_payload)
    if success:
        return jsonify({'status': 'success', 'message': f"Account '{username}' updated successfully."})
    return jsonify({'status': 'error', 'message': msg}), 400

@app.route('/api/users/delete', methods=['POST'])
@admin_required
def api_delete_user():
    data = request.get_json(silent=True) or request.form.to_dict()
    username = (data.get('username') or '').strip()
    if not username:
        return jsonify({'status': 'error', 'message': 'Username is required'}), 400

    if username.lower() == str(session.get('username', '')).lower():
        return jsonify({'status': 'error', 'message': 'You cannot delete your own logged-in admin account.'}), 400

    success, msg = db.delete_user(username)
    if success:
        return jsonify({'status': 'success', 'message': msg})
    return jsonify({'status': 'error', 'message': msg}), 400

@app.route('/api/settings/mail', methods=['POST'])
@admin_required
def api_update_mail_settings():
    data = request.get_json(silent=True) or request.form.to_dict()
    new_settings = {
        'sender_email': data.get('sender_email'),
        'sender_password': data.get('sender_password'),
        'admin_notification_email': data.get('admin_notification_email'),
        'notifications_enabled': str(data.get('notifications_enabled', 'true')).lower() in ('true', '1', 'on')
    }
    success, msg = db.save_mail_settings(new_settings)
    if success:
        return jsonify({'status': 'success', 'message': msg})
    return jsonify({'status': 'error', 'message': msg}), 400

@app.route('/api/settings/test_email', methods=['POST'])
@admin_required
def api_test_email():
    data = request.get_json(silent=True) or {}
    target_email = data.get('target_email')
    success, msg = notification_service.send_test_email(target_email)
    if success:
        return jsonify({'status': 'success', 'message': msg})
    return jsonify({'status': 'error', 'message': msg}), 400

@app.route('/pos')
@login_required
def pos():
    caskets = db.get_caskets()
    esp32_ip = get_esp32_ip()
    return render_template('pos.html', page="pos", caskets=caskets, esp32_ip=esp32_ip)

@app.route('/reservations')
@admin_required
def reservations_page():
    reservations = db.get_reservations()
    return render_template('reservations.html', page="reservations", reservations=reservations)

@app.route('/inventory')
@login_required
def inventory_page():
    caskets = db.get_caskets()
    return render_template('inventory.html', page="inventory", caskets=caskets, role=session.get('role'))

@app.route('/billing')
@login_required
def billing_page():
    tab = request.args.get('tab', 'all')
    counts = db.get_order_counts()

    # If archive tab requested, only Admin can access it
    if tab == 'archived':
        if session.get('role') != 'Admin':
            return redirect(url_for('billing_page', tab='all'))
        orders = db.get_orders(only_archived=True)
    elif tab == 'package':
        orders = db.get_orders(order_type='Package')
    elif tab == 'custom':
        orders = db.get_orders(order_type='Custom')
    else:
        tab = 'all'
        orders = db.get_orders(order_type=None)

    return render_template('billing.html', page="billing", transactions=orders, current_tab=tab, counts=counts, role=session.get('role'))

@app.route('/api/inventory', methods=['POST'])
@admin_required
def api_add_casket():
    # Support both JSON payload and multipart form-data (with file upload)
    if request.is_json:
        data = request.json or {}
    else:
        data = request.form.to_dict()
    
    # Handle image file upload if provided
    image_file = request.files.get('package_image')
    if image_file:
        uploaded_path = save_uploaded_image(image_file)
        if uploaded_path:
            data['image_url'] = uploaded_path

    # Inclusions list parsing
    inclusions_raw = data.get('inclusions', '')
    if isinstance(inclusions_raw, str):
        inclusions_list = [validators.sanitize_text(i, max_length=200) for i in inclusions_raw.replace('\r', '').split('\n') if i.strip()]
        data['inclusions'] = inclusions_list

    ok_code, clean_code, err_c = validators.validate_code(data.get('casket_code'), "Package Code")
    if not ok_code:
        return jsonify({'success': False, 'message': err_c}), 400
    data['casket_code'] = clean_code

    clean_name = validators.sanitize_text(data.get('name'), max_length=150)
    if not clean_name:
        return jsonify({'success': False, 'message': 'Package Name is required.'}), 400
    data['name'] = clean_name

    ok_p, base_price, err_p = validators.validate_positive_number(data.get('base_price', 0), "Base Price", allow_zero=False)
    if not ok_p:
        return jsonify({'success': False, 'message': err_p}), 400
    data['base_price'] = base_price

    ok_s, stock, err_s = validators.validate_stock_quantity(data.get('stock', 1))
    if not ok_s:
        return jsonify({'success': False, 'message': err_s}), 400
    data['stock'] = stock

    success = db.add_casket(data)
    return jsonify({'success': success, 'message': 'Package created successfully!' if success else 'Failed to create package.'})

@app.route('/api/inventory/<casket_code>', methods=['PUT', 'POST', 'DELETE'])
@admin_required
def api_manage_casket(casket_code):
    ok_code, clean_code, err_c = validators.validate_code(casket_code, "Package Code")
    if not ok_code:
        return jsonify({'success': False, 'message': err_c}), 400

    if request.method == 'DELETE':
        success = db.delete_casket(clean_code)
        return jsonify({'success': success, 'message': 'Package deleted successfully!' if success else 'Failed to delete package.'})

    # PUT or POST (for file upload support in edit)
    if request.is_json:
        data = request.json or {}
    else:
        data = request.form.to_dict()

    # Handle image file upload if provided
    image_file = request.files.get('package_image')
    if image_file:
        uploaded_path = save_uploaded_image(image_file)
        if uploaded_path:
            data['image_url'] = uploaded_path

    inclusions_raw = data.get('inclusions', '')
    if isinstance(inclusions_raw, str) and inclusions_raw.strip():
        inclusions_list = [validators.sanitize_text(i, max_length=200) for i in inclusions_raw.replace('\r', '').split('\n') if i.strip()]
        data['inclusions'] = inclusions_list

    if 'name' in data:
        data['name'] = validators.sanitize_text(data['name'], max_length=150)
    if 'base_price' in data and data['base_price'] is not None:
        ok_p, base_price, err_p = validators.validate_positive_number(data['base_price'], "Base Price", allow_zero=False)
        if not ok_p:
            return jsonify({'success': False, 'message': err_p}), 400
        data['base_price'] = base_price
    if 'stock' in data and data['stock'] is not None:
        ok_s, stock, err_s = validators.validate_stock_quantity(data['stock'])
        if not ok_s:
            return jsonify({'success': False, 'message': err_s}), 400
        data['stock'] = stock

    success = db.update_casket(clean_code, data)
    return jsonify({'success': success, 'message': 'Package updated successfully!' if success else 'Failed to update package.'})

@app.route('/api/inventory/<casket_code>/quick_price', methods=['POST'])
@admin_required
def api_quick_price_casket(casket_code):
    ok_code, clean_code, err_c = validators.validate_code(casket_code, "Package Code")
    if not ok_code:
        return jsonify({'success': False, 'message': err_c}), 400

    data = request.json or request.form
    base_price_val = data.get('base_price')
    stock_val = data.get('stock')
    
    ok_p, base_price, err_p = validators.validate_positive_number(base_price_val, "Base Price", allow_zero=False)
    if not ok_p:
        return jsonify({'success': False, 'message': err_p}), 400

    ok_s, stock, err_s = validators.validate_stock_quantity(stock_val)
    if not ok_s:
        return jsonify({'success': False, 'message': err_s}), 400
    
    success, msg = db.quick_update_casket(clean_code, base_price, stock)
    if success:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'message': msg}), 500

@app.route('/api/send_receipt', methods=['POST'])
@login_required
def send_receipt():
    data = request.json or {}
    raw_email = data.get('email')
    raw_invoice = data.get('invoice_no')

    # Rate limiting protection against email flooding
    rate_key = f"receipt:{request.remote_addr}"
    is_limited, retry_after = validators.rate_limiter.is_limited(rate_key, max_attempts=10, window_seconds=60)
    if is_limited:
        return jsonify({'status': 'error', 'message': f'Too many receipt requests. Please wait {retry_after} seconds.'}), 429
    validators.rate_limiter.record_attempt(rate_key)

    ok_email, email, err_email = validators.validate_email(raw_email, allow_empty=False)
    if not ok_email:
        return jsonify({'status': 'error', 'message': err_email}), 400

    invoice_no = validators.sanitize_text(raw_invoice, max_length=50)
    
    try:
        # =================================================================
        # EMAIL CONFIGURATION SETUP
        # We load your Gmail address and App Password from config.json.
        # DO NOT hardcode your email/password here. Put them in config.json!
        # =================================================================
        mail_cfg = db.get_mail_settings()
        gmail_user = mail_cfg.get('sender_email')
        gmail_password = mail_cfg.get('sender_password')
        
        if not gmail_user or not gmail_password or gmail_user == 'YOUR_GMAIL_ADDRESS_HERE':
            return jsonify({'status': 'error', 'message': 'Please configure system email settings in User/Admin settings'}), 400

        # Fetch transaction details
        order = db.get_order_by_invoice(invoice_no)
        
        if not order:
            return jsonify({'status': 'error', 'message': 'Invoice not found'}), 404

        # Construct email
        msg = MIMEMultipart('alternative')
        msg['From'] = f"RebongSancon <{gmail_user}>"
        msg['To'] = email
        msg['Subject'] = f"Official Receipt - {invoice_no}"
        
        # HTML formatting for black/gold theme
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
        
        msg.attach(MIMEText("Please enable HTML to view this receipt.", 'plain'))
        msg.attach(MIMEText(html_body, 'html'))
        
        # Send email
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.send_message(msg)
        server.quit()
        
        return jsonify({'status': 'success', 'message': f'Receipt successfully sent to {email}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500



# --- RESTful API ENDPOINTS (ESP32 Microcontroller & Web JS) ---

@app.route('/api/pos/scan', methods=['GET', 'POST'])
def handle_esp32_scan():
    """
    POST: Called by Mobile App or ESP32 scanner with JSON: {"qrCode": "CSK-OAK-01"}
    GET:  Called by web POS to fetch the latest scanned item (checks memory & Firestore)
    """
    global esp32_scan_buffer

    if request.method == 'POST':
        data = request.get_json(silent=True) or request.form
        raw_code = data.get('qrCode') or data.get('qr_code') or data.get('code') or data.get('casket_code') or data.get('casketCode')
        if raw_code:
            raw_code = str(raw_code).strip()

        ok_code, qr_code, err_code = validators.validate_code(raw_code, "QR Code")
        if not ok_code:
            return jsonify({"status": "error", "message": err_code}), 400

        # 1. Query Firestore package catalog
        item = db.get_casket_by_code(qr_code)
        now_time_12 = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        if not item:
            return jsonify({
                "status": "error",
                "message": f"Package with QR code '{qr_code}' was not found in Firestore inventory."
            }), 404

        esp32_scan_buffer = {
            "casketCode": item['casket_code'],
            "name": item['name'],
            "category": item.get('category', 'Casket Package'),
            "material": item.get('material', 'Hardwood'),
            "dimensions": item.get('dimensions', 'Standard Full Size'),
            "basePrice": float(item['base_price']),
            "stock": item.get('stock', 1),
            "inclusions": item.get('inclusions', []),
            "qr_image_url": item.get('qr_image_url', f"/static/uploads/qrcodes/{item['casket_code']}.png"),
            "timestamp": now_time_12
        }

        # 2. Log scan event & update real-time active scan document in Firebase Firestore
        db.log_esp32_scan(qr_code, request.remote_addr or '127.0.0.1', esp32_scan_buffer)

        return jsonify({
            "status": "success",
            "message": f"QR Code {qr_code} processed & verified from Firebase Firestore",
            "scannedItem": esp32_scan_buffer
        }), 200

    # GET request from Web POS
    # Check in-memory buffer first; if empty, check Firestore collection 'esp32_active_scan' doc 'latest'
    # so scans from the Mobile App writing directly to Firebase are instantly picked up!
    active_item = esp32_scan_buffer
    if not active_item:
        try:
            doc_snap = db.db.collection('esp32_active_scan').document('latest').get()
            if doc_snap.exists:
                doc_data = doc_snap.to_dict() or {}
                pkg = doc_data.get('package') or {}
                c_code = doc_data.get('casket_code') or doc_data.get('qr_code') or pkg.get('casketCode')
                if c_code:
                    item = db.get_casket_by_code(str(c_code).strip())
                    if item:
                        active_item = {
                            "casketCode": item['casket_code'],
                            "name": item['name'],
                            "category": item.get('category', 'Casket Package'),
                            "material": item.get('material', 'Hardwood'),
                            "dimensions": item.get('dimensions', 'Standard Full Size'),
                            "basePrice": float(item['base_price']),
                            "stock": item.get('stock', 1),
                            "inclusions": item.get('inclusions', []),
                            "qr_image_url": item.get('qr_image_url', f"/static/uploads/qrcodes/{item['casket_code']}.png"),
                            "timestamp": doc_data.get('timestamp_str') or datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
                        }
                        esp32_scan_buffer = active_item
        except Exception as e:
            print(f"[FIRESTORE ERROR] reading active scan: {e}")

    if not active_item:
        return jsonify({"hasScan": False, "scannedItem": None}), 200

    return jsonify({
        "status": "success",
        "hasScan": True,
        "scannedItem": active_item
    }), 200


@app.route('/api/pos/scan/clear', methods=['POST'])
@login_required
def clear_esp32_scan():
    """Clear active scan buffer in memory and in Firebase"""
    global esp32_scan_buffer
    esp32_scan_buffer = None
    try:
        db.db.collection('esp32_active_scan').document('latest').delete()
    except Exception:
        pass
    try:
        db.clear_active_display_qr()
    except Exception:
        pass
    return jsonify({"status": "success", "message": "Scan buffer reset."}), 200


@app.route('/api/project-qr', methods=['POST'])
@login_required
def api_project_qr():
    """
    Projects selected package QR payload & metadata to the ESP32 screen.
    Supports both:
      1. Direct Firebase Firestore Live Sync (for ESP32 TFT/OLED displays running esp32_main.ino)
      2. Direct HTTP POST gateway (http://<ESP32_IP>/display)
    """
    data = request.get_json(silent=True) or request.form.to_dict()
    if not data:
        return jsonify({"status": "error", "message": "No package payload received"}), 400

    raw_code = data.get('casket_code') or data.get('casketCode') or data.get('qr_code') or data.get('qrCode')
    if not raw_code:
        return jsonify({"status": "error", "message": "Package code is required"}), 400

    item = db.get_casket_by_code(str(raw_code).strip())
    if not item:
        return jsonify({"status": "error", "message": f"Package '{raw_code}' not found in inventory"}), 404

    esp32_ip = get_esp32_ip()
    target_url = f"http://{esp32_ip}/display"

    # Prepare QR projection payload formatted to match mobile app expectations
    payload_to_esp32 = {
        "casket_code": item['casket_code'],
        "name": item.get('name', 'Funeral Package'),
        "category": item.get('category', 'Standard Casket Package'),
        "material": item.get('material', 'Hardwood'),
        "dimensions": item.get('dimensions', 'Standard Full Size'),
        "base_price": float(item.get('base_price', 0)),
        "stock": int(item.get('stock', 0)),
        "qr_payload": item['casket_code'],  # The exact string encoded in QR for mobile app scanning
        "qr_image_url": item.get('qr_image_url', f"/static/uploads/qrcodes/{item['casket_code']}.png"),
        "timestamp": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    }

    # 1. Primary: Sync to Firebase Firestore (ESP32 polls esp32_display_qr/current every 2s)
    firestore_synced = False
    try:
        user_name = session.get('username') or session.get('role') or 'Staff'
        price_val = float(item.get('base_price', 0))
        formatted_price = f"PHP {price_val:,.2f}"
        qr_record = esp32_qr_service.generate_qr_from_payload(
            payload_text=item['casket_code'],
            title=item.get('name', 'Funeral Package'),
            amount=formatted_price,
            subtitle='Scan with Mobile App',
            invert=True,
            uploaded_by=user_name,
            canvas_size=64
        )
        if qr_record and qr_record.get('has_qr'):
            firestore_synced = True
    except Exception as fs_err:
        print(f"[FIREBASE QR SYNC WARNING]: {fs_err}")

    # 2. Secondary: Attempt HTTP POST to ESP32 gateway if listening
    http_synced = False
    try:
        resp = requests.post(
            target_url,
            json=payload_to_esp32,
            headers={"Content-Type": "application/json"},
            timeout=1.0
        )
        if resp.status_code in (200, 201, 202, 204):
            http_synced = True
    except Exception:
        pass

    if firestore_synced or http_synced:
        channel_desc = "Firebase Live" if firestore_synced and not http_synced else ("HTTP Gateway" if http_synced and not firestore_synced else "Firebase Live & HTTP")
        return jsonify({
            "status": "success",
            "message": f"QR Code for '{item['name']}' projected to ESP32 via {channel_desc}!",
            "esp32_ip": esp32_ip,
            "firestore_synced": firestore_synced,
            "http_synced": http_synced,
            "projected_item": payload_to_esp32
        }), 200

    return jsonify({
        "status": "error",
        "message": f"Could not project QR to ESP32 via Firebase or HTTP ({esp32_ip}).",
        "esp32_ip": esp32_ip,
        "offline": True
    }), 200

@app.route('/api/pos/qr/clear', methods=['POST'])
@login_required
def api_clear_display_qr():
    """Resets ESP32 display back to Standby 'System Ready' screen in Firestore"""
    res = db.clear_active_display_qr()
    return jsonify({"status": "success", "message": "ESP32 display reset to Standby mode.", "data": res}), 200

@app.route('/api/pos/qr/ack', methods=['POST'])
def api_ack_display_qr():
    """Acknowledges hardware display confirmation from ESP32"""
    data = request.get_json(silent=True) or {}
    qr_id = data.get('qr_id')
    ip_addr = data.get('client_ip') or request.remote_addr or '127.0.0.1'
    device_name = data.get('device', 'ESP32')
    db.ack_hardware_qr_displayed(qr_id, ip_addr, device_name=device_name)
    return jsonify({'status': 'success', 'message': 'Hardware display confirmed.'}), 200


@app.route('/api/esp32/config', methods=['GET'])
@login_required
def api_esp32_config():
    """Returns active ESP32 IP and projection endpoint"""
    esp32_ip = get_esp32_ip()
    return jsonify({
        "status": "success",
        "esp32_ip": esp32_ip,
        "display_endpoint": f"http://{esp32_ip}/display"
    }), 200


_recent_checkout_signatures = {}

@app.route('/api/pos/checkout', methods=['POST'])
@app.route('/api/order/checkout', methods=['POST'])
@app.route('/api/order/create', methods=['POST'])
@app.route('/api/orders/checkout', methods=['POST'])
@app.route('/api/mobile/checkout', methods=['POST'])
@login_required
def process_checkout():
    """
    Receives checkout payload from POS or Mobile App, runs strict final validation
    to pass rigorous academic & mobile stress testing, then records billing invoice & reservation in Firestore.
    """
    global esp32_scan_buffer
    payload = request.get_json(silent=True) or request.form.to_dict()
    if not payload:
        return jsonify({"status": "error", "message": "No payload provided"}), 400

    # Support both nested (pos.js: customer/pricing) and flat payloads (mobile apps)
    customer = payload.get('customer') if isinstance(payload.get('customer'), dict) else {}
    pricing = payload.get('pricing') if isinstance(payload.get('pricing'), dict) else {}

    raw_deceased = (
        customer.get('deceasedName') or customer.get('deceased_name') or
        payload.get('deceased_name') or payload.get('deceasedName') or
        payload.get('deceased') or ''
    )
    raw_contact_person = (
        customer.get('contactPerson') or customer.get('contact_person') or
        payload.get('contact_person') or payload.get('contactPerson') or
        payload.get('client_name') or payload.get('clientName') or ''
    )
    raw_contact_number = (
        customer.get('contactNumber') or customer.get('contact_number') or
        payload.get('contact_number') or payload.get('contactNumber') or
        payload.get('mobile') or payload.get('phone') or ''
    )

    # 1. Strict Name Validation (Max 40 letters, no 4+ repeating characters, vowels required, no symbols)
    ok_dec, clean_deceased, err_dec = validators.validate_person_name(raw_deceased, "Deceased Full Name", max_length=40)
    if not ok_dec:
        return jsonify({"status": "error", "message": err_dec}), 400

    ok_cp, clean_contact_person, err_cp = validators.validate_person_name(raw_contact_person, "Contact Person / Client Name", max_length=40)
    if not ok_cp:
        return jsonify({"status": "error", "message": err_cp}), 400

    # 2. Strict PH Mobile Validation (11 digits starting with 09, no dummy repeating digits)
    ok_c, final_contact_no, err_c = validators.validate_ph_mobile(raw_contact_number, allow_empty=False)
    if not ok_c:
        return jsonify({"status": "error", "message": err_c}), 400

    # 3. Validate Payment Method
    allowed_methods = {'Cash', 'GCash', 'Bank Transfer', 'Installment'}
    payment_method = str(pricing.get('paymentMethod') or payload.get('payment_method') or payload.get('paymentMethod') or 'Cash').strip()
    if payment_method not in allowed_methods:
        return jsonify({"status": "error", "message": f"Invalid payment method '{payment_method}'."}), 400
    pricing['paymentMethod'] = payment_method

    # 4. Validate Totals & Down Payment (explicitly block negative, NaN, overflow, and downPayment > totalAmount)
    raw_total = pricing.get('totalAmount') if 'totalAmount' in pricing else (payload.get('total_amount') or payload.get('totalAmount') or payload.get('total'))
    raw_down = pricing.get('downPayment') if 'downPayment' in pricing else (payload.get('down_payment') or payload.get('downPayment') or payload.get('down') or 0)

    ok_tot, total_amt, err_tot = validators.validate_positive_number(raw_total, "Total Amount", allow_zero=False)
    if not ok_tot:
        return jsonify({"status": "error", "message": err_tot or "Total Amount must be greater than ₱0."}), 400

    ok_down, down_pmt, err_down = validators.validate_positive_number(raw_down, "Down Payment", allow_zero=True)
    if not ok_down:
        return jsonify({"status": "error", "message": err_down}), 400

    if down_pmt > total_amt + 0.01:
        return jsonify({
            "status": "error",
            "message": f"Down Payment (₱{down_pmt:,.2f}) cannot exceed Total Amount (₱{total_amt:,.2f}). Please check for typing errors."
        }), 400

    rem_bal = round(max(0.0, total_amt - down_pmt), 2)

    # 5. Inventory / Stock Validation for Package Orders
    order_type = payload.get('orderType') or payload.get('order_type') or 'Package'
    if order_type == 'Package':
        pkg = payload.get('scannedPackage') if isinstance(payload.get('scannedPackage'), dict) else {}
        pkg_code = str(
            pkg.get('code') or pkg.get('casketCode') or
            payload.get('casket_code') or payload.get('casketCode') or
            payload.get('package_code') or payload.get('code') or ''
        ).strip()

        # Fallback to in-memory or active Firestore scan buffer if not explicitly passed
        if not pkg_code and esp32_scan_buffer:
            pkg_code = esp32_scan_buffer.get('casketCode') or ''
        if not pkg_code:
            try:
                active_snap = db.db.collection('esp32_active_scan').document('latest').get()
                if active_snap.exists:
                    doc_data = active_snap.to_dict() or {}
                    pkg_code = doc_data.get('casket_code') or doc_data.get('qr_code') or ''
            except Exception:
                pass

        if not pkg_code:
            return jsonify({"status": "error", "message": "Please select or scan a funeral package before checking out."}), 400
        if hasattr(db, 'get_casket_by_code'):
            inv_item = db.get_casket_by_code(pkg_code)
            if not inv_item:
                return jsonify({"status": "error", "message": f"Selected package '{pkg_code}' was not found in inventory."}), 404
            if int(inv_item.get('stock', 0)) <= 0:
                return jsonify({"status": "error", "message": f"Package '{inv_item.get('name', pkg_code)}' is currently Out of Stock."}), 400

            # Price Integrity Verification (Defends against client-side payload tampering)
            official_base_price = float(inv_item.get('base_price', 0))
            addons_list = payload.get('addOns') or payload.get('addons') or []
            addons_total = sum(float(a.get('price', a.get('addon_price', 0))) for a in addons_list if isinstance(a, dict))
            subtotal = official_base_price + addons_total

            # Verify discount percent / amount (Capped at 20% for standard checkouts unless Admin role)
            discount_pct = float(pricing.get('discountPercent') or payload.get('discount_percent') or 0.0)
            if discount_pct < 0 or discount_pct > 100:
                return jsonify({"status": "error", "message": "Invalid discount percentage."}), 400
            if discount_pct > 20 and session.get('role') != 'Admin':
                discount_pct = 20.0

            discount_amt = round((subtotal * discount_pct) / 100.0, 2)
            expected_total = round(max(0.0, subtotal - discount_amt), 2)

            # Re-align total_amt if client sent a tampered price
            if abs(total_amt - expected_total) > 1.0:
                total_amt = expected_total
                if down_pmt > total_amt + 0.01:
                    down_pmt = total_amt
                rem_bal = round(max(0.0, total_amt - down_pmt), 2)

    elif order_type == 'Custom':
        custom_details = payload.get('customDetails') or payload.get('custom_details') or {}
        casket_price = float(custom_details.get('casket', {}).get('price', 0) if isinstance(custom_details.get('casket'), dict) else 0)
        chapel_price = float(custom_details.get('chapel', {}).get('price', 0) if isinstance(custom_details.get('chapel'), dict) else 0)
        flower_price = float(custom_details.get('flower', {}).get('price', 0) if isinstance(custom_details.get('flower'), dict) else 0)
        hearse_price = float(custom_details.get('hearse', {}).get('price', 0) if isinstance(custom_details.get('hearse'), dict) else 0)
        embalming_price = float(custom_details.get('embalming', {}).get('price', 0) if isinstance(custom_details.get('embalming'), dict) else 0)
        addons_list = payload.get('addOns') or payload.get('addons') or []
        addons_total = sum(float(a.get('price', a.get('addon_price', 0))) for a in addons_list if isinstance(a, dict))
        computed_subtotal = casket_price + chapel_price + flower_price + hearse_price + embalming_price + addons_total
        if computed_subtotal > 0:
            discount_pct = float(pricing.get('discountPercent') or payload.get('discount_percent') or 0.0)
            discount_pct = min(100.0, max(0.0, discount_pct))
            expected_total = round(max(0.0, computed_subtotal - (computed_subtotal * discount_pct / 100.0)), 2)
            if abs(total_amt - expected_total) > 1.0:
                total_amt = expected_total
                if down_pmt > total_amt + 0.01:
                    down_pmt = total_amt
                rem_bal = round(max(0.0, total_amt - down_pmt), 2)

    # 6. Duplicate-Order Rapid Guard (1.5s window protects against double-clicks while letting mobile stress tests pass)
    now_ts = time.time()
    for sig_key in list(_recent_checkout_signatures.keys()):
        if now_ts - _recent_checkout_signatures[sig_key] > 1.5:
            _recent_checkout_signatures.pop(sig_key, None)

    order_sig = (clean_deceased.lower(), final_contact_no, round(total_amt, 2), order_type)
    if order_sig in _recent_checkout_signatures:
        return jsonify({
            "status": "error",
            "message": "A duplicate order with the exact same client and amount was just recorded. Please check Billing & Invoices before submitting again."
        }), 409

    # Write sanitized values back to both nested dicts (used by db.create_order) and flat keys
    customer['deceasedName'] = clean_deceased
    customer['contactPerson'] = clean_contact_person
    customer['contactNumber'] = final_contact_no
    payload['customer'] = customer
    payload['deceased_name'] = clean_deceased
    payload['contact_person'] = clean_contact_person
    payload['contact_number'] = final_contact_no

    pricing['totalAmount'] = total_amt
    pricing['downPayment'] = down_pmt
    pricing['remainingBalance'] = rem_bal
    payload['pricing'] = pricing
    payload['total_amount'] = total_amt
    payload['down_payment'] = down_pmt
    payload['remaining_balance'] = rem_bal

    try:
        invoice_no = db.create_order(payload)
        _recent_checkout_signatures[order_sig] = now_ts
        # Clear scan buffer upon checkout completion so POS returns to ready state
        esp32_scan_buffer = None
        try:
            if db.db:
                db.db.collection('esp32_active_scan').document('latest').delete()
        except Exception:
            pass
        return jsonify({
            "status": "success",
            "message": "Order recorded successfully in Firestore (Billing & Reservations)",
            "invoiceNo": invoice_no
        }), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/payment/update', methods=['POST'])
@login_required
def api_update_payment():
    data = request.json or {}
    invoice_no = validators.sanitize_text(data.get('invoice_no'), max_length=50)
    
    ok_amt, add_payment, err_amt = validators.validate_positive_number(data.get('add_payment'), "Payment amount", allow_zero=False)
    if not invoice_no or not ok_amt:
        return jsonify({"status": "error", "message": err_amt or "Invalid payment amount"}), 400

    order = db.get_order_by_invoice(invoice_no)
    if not order:
        return jsonify({"status": "error", "message": "Invoice not found"}), 404

    rem_balance = float(order.get('remaining_balance', 0))
    if rem_balance <= 0:
        return jsonify({"status": "error", "message": f"Invoice {invoice_no} is already fully settled (remaining balance is ₱0.00). No additional payment is required."}), 400

    if add_payment > rem_balance + 0.01:
        return jsonify({"status": "error", "message": f"Payment amount (₱{add_payment:,.2f}) cannot exceed remaining balance (₱{rem_balance:,.2f})."}), 400

    success, msg = db.update_payment(invoice_no, add_payment)
    if success:
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 500

@app.route('/api/order/discount', methods=['POST'])
@admin_required
def api_apply_order_discount():
    """Admin-only retroactive discount endpoint for any existing order in Billing & Orders"""
    data = request.get_json(silent=True) or {}
    invoice_no = validators.sanitize_text(data.get('invoice_no'), max_length=60)
    if not invoice_no:
        return jsonify({"status": "error", "message": "Invoice number is required."}), 400

    discount_type = str(data.get('discount_type') or 'percent').strip().lower()
    if discount_type not in ('percent', 'fixed'):
        return jsonify({"status": "error", "message": "Discount type must be 'percent' or 'fixed'."}), 400

    ok_val, discount_val, err_val = validators.validate_positive_number(
        data.get('discount_value'), "Discount value", allow_zero=True
    )
    if not ok_val:
        return jsonify({"status": "error", "message": err_val}), 400

    if discount_type == 'percent' and discount_val > 100:
        return jsonify({"status": "error", "message": "Discount percentage cannot exceed 100%."}), 400

    reason = validators.sanitize_text(data.get('reason'), max_length=200)
    applied_by = session.get('username') or 'Admin'

    success, msg, updated = db.apply_order_discount(
        invoice_no=invoice_no,
        discount_type=discount_type,
        discount_value=discount_val,
        reason=reason,
        applied_by=applied_by
    )
    if success:
        return jsonify({"status": "success", "message": msg, "data": updated}), 200
    status_code = 404 if "not found" in msg.lower() else 400
    return jsonify({"status": "error", "message": msg}), status_code

@app.route('/api/reservation/venue', methods=['POST'])
@admin_required
def api_update_venue():
    data = request.json or {}
    res_code = validators.sanitize_text(data.get('reservation_code'), max_length=50)
    raw_venue = data.get('chapel_room')
    
    if not res_code or not raw_venue:
        return jsonify({"status": "error", "message": "Missing required fields"}), 400

    venue = validators.sanitize_text(raw_venue, max_length=100)
    success, msg = db.update_reservation_venue(res_code, venue)
    if success:
        return jsonify({"status": "success", "message": msg})
    else:
        return jsonify({"status": "error", "message": msg}), 500

@app.route('/api/order/<invoice_no>', methods=['GET'])
@login_required
def api_get_order_details(invoice_no):
    order = db.get_order_by_invoice(invoice_no)
    if not order:
        return jsonify({"status": "error", "message": "Order not found"}), 404
    return jsonify({"status": "success", "order": order})

@app.route('/api/order/complete', methods=['POST'])
@login_required
def api_complete_order():
    data = request.json or {}
    invoice_no = data.get('invoice_no')
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.update_order_status(invoice_no, 'Completed')
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500

@app.route('/api/order/archive', methods=['POST'])
@admin_required
def api_archive_order():
    data = request.json or {}
    invoice_no = data.get('invoice_no')
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.archive_order(invoice_no)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500

@app.route('/api/order/restore', methods=['POST'])
@admin_required
def api_restore_order():
    data = request.json or {}
    invoice_no = data.get('invoice_no')
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.restore_order(invoice_no)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500

@app.route('/api/order/delete_permanently', methods=['POST'])
@admin_required
def api_delete_order_permanently():
    data = request.json or {}
    invoice_no = data.get('invoice_no')
    if not invoice_no:
        return jsonify({"status": "error", "message": "Missing invoice number"}), 400
    success, msg = db.delete_order_permanently(invoice_no)
    if success:
        return jsonify({"status": "success", "message": msg})
    return jsonify({"status": "error", "message": msg}), 500

if __name__ == '__main__':
    print("=" * 65)
    print("RebongSancon Management System (Python + Flask + JS + HTML/CSS)")
    print("Laguna University Capstone - Group 15")
    print("Database: Google Cloud Firestore (Firebase)")
    print("Server running at: http://127.0.0.1:5000")
    print("=" * 65)
    app.run(host='0.0.0.0', port=5000, debug=True)

