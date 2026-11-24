import os
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import db
import validators

IS_CLOUD = bool(os.environ.get("K_SERVICE") or os.environ.get("FUNCTION_TARGET"))


def send_attendance_email(username, action, attendance_record=None, timeout=8):
    """
    Sends an automated email notification to all Admin email addresses configured in the system
    whenever an employee times in (Clock In) or times out (Clock Out).
    """
    try:
        settings = db.get_mail_settings()
        if not settings.get("notifications_enabled", True):
            print("[ATTENDANCE NOTIF] Notifications are currently disabled in system settings.")
            return False, "Notifications are disabled."

        sender_email = (settings.get("sender_email") or "").strip()
        sender_password = (settings.get("sender_password") or "").strip()

        if not sender_email or not sender_password:
            print("[ATTENDANCE NOTIF ERROR] Missing sender email or app password in system settings.")
            return False, "Missing sender email or password in system settings."

        admin_emails = db.get_admin_emails()
        if not admin_emails:
            print("[ATTENDANCE NOTIF WARNING] No admin email address registered in the system. Notification skipped.")
            return False, "No admin email configured in the system."

        user = db.get_user_by_username(username) or {}
        employee_name = validators.sanitize_text(user.get("full_name") or username, max_length=100)
        employee_email = validators.sanitize_text(user.get("email") or "No email recorded in system", max_length=150)
        employee_contact = validators.sanitize_text(user.get("contact_no") or "No contact number", max_length=50)

        att = attendance_record or {}
        log_date = validators.sanitize_text(att.get("log_date") or db.now_pht().strftime("%Y-%m-%d"), max_length=30)
        time_in = validators.sanitize_text(att.get("time_in") or "--", max_length=30)
        time_out = validators.sanitize_text(att.get("time_out") or "--", max_length=30)
        duration = att.get("duration") or att.get("duration_hours") or 0.0

        is_clock_in = action.upper() in ("CLOCK_IN", "TIME_IN")

        if is_clock_in:
            subject = f"⏱️ [Attendance Alert] Staff Clock In: {employee_name} ({time_in})"
            badge_color = "#10b981"
            badge_bg = "#ecfdf5"
            badge_border = "#a7f3d0"
            badge_text = "● ON DUTY — CLOCKED IN"
            action_title = "Staff Clock In (Time In)"
            action_desc = f"Staff employee <strong>{employee_name}</strong> (<code>@{username}</code>) has recorded their <strong>Time In</strong>."
            status_text = "On Duty"
        else:
            subject = f"🏁 [Attendance Alert] Staff Clock Out: {employee_name} ({time_out})"
            badge_color = "#d97706"
            badge_bg = "#fffbeb"
            badge_border = "#fde68a"
            badge_text = "● SHIFT COMPLETED — CLOCKED OUT"
            action_title = "Staff Clock Out (Time Out)"
            action_desc = f"Staff employee <strong>{employee_name}</strong> (<code>@{username}</code>) has recorded their <strong>Time Out</strong>."
            status_text = "Completed"

        html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
  <style>
    body {{
      margin: 0;
      padding: 24px;
      background-color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #1e293b;
    }}
    .email-container {{
      max-width: 600px;
      margin: 0 auto;
      background-color: #ffffff;
      border-radius: 16px;
      overflow: hidden;
      border: 1px solid #e2e8f0;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05);
    }}
    .header {{
      background-color: #000000;
      color: #ffffff;
      padding: 28px 24px;
      text-align: center;
      border-bottom: 4px solid #eab308;
    }}
    .header h1 {{
      margin: 0;
      font-size: 22px;
      font-weight: 800;
      letter-spacing: 0.5px;
      color: #ffffff;
    }}
    .header p {{
      margin: 6px 0 0 0;
      font-size: 12px;
      font-weight: 600;
      color: #eab308;
      text-transform: uppercase;
      letter-spacing: 1.5px;
    }}
    .content {{
      padding: 30px 24px;
    }}
    .badge {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.5px;
      color: {badge_color};
      background-color: {badge_bg};
      border: 1px solid {badge_border};
      margin-bottom: 14px;
    }}
    .action-title {{
      font-size: 19px;
      font-weight: 800;
      color: #0f172a;
      margin: 0 0 8px 0;
    }}
    .action-desc {{
      font-size: 14px;
      color: #64748b;
      line-height: 1.5;
      margin: 0 0 24px 0;
    }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      overflow: hidden;
    }}
    .data-table tr:nth-child(even) {{
      background-color: #f8fafc;
    }}
    .data-table th {{
      text-align: left;
      padding: 11px 16px;
      font-size: 12px;
      font-weight: 700;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      width: 38%;
      border-bottom: 1px solid #e2e8f0;
    }}
    .data-table td {{
      padding: 11px 16px;
      font-size: 13px;
      font-weight: 500;
      color: #1e293b;
      border-bottom: 1px solid #e2e8f0;
    }}
    .footer {{
      background-color: #0f172a;
      color: #94a3b8;
      padding: 20px 24px;
      text-align: center;
      font-size: 11px;
      line-height: 1.6;
    }}
  </style>
</head>
<body>
  <div class="email-container">
    <div class="header">
      <h1>RebongSancon Funeral Services</h1>
      <p>Staff Attendance & Daily Time Record Alert</p>
    </div>
    <div class="content">
      <div class="badge">{badge_text}</div>
      <h2 class="action-title">{action_title}</h2>
      <p class="action-desc">{action_desc}</p>
      <table class="data-table">
        <tr><th>Employee Name</th><td><strong>{employee_name}</strong></td></tr>
        <tr><th>Username</th><td><code>@{username}</code></td></tr>
        <tr><th>Employee Email</th><td><strong>{employee_email}</strong></td></tr>
        <tr><th>Mobile Contact</th><td>{employee_contact}</td></tr>
        <tr><th>Date</th><td>{log_date}</td></tr>
        <tr><th>Time In (Clock In)</th><td style="color: #059669; font-weight: 700;">{time_in}</td></tr>
        {'<tr><th>Time Out (Clock Out)</th><td style="color: #d97706; font-weight: 700;">' + str(time_out) + '</td></tr>' if not is_clock_in else ''}
        {'<tr><th>Shift Duration</th><td><strong>' + str(duration) + ' hours</strong></td></tr>' if not is_clock_in else ''}
        <tr>
          <th>Attendance Status</th>
          <td><span style="font-weight: 700; color: {'#059669' if is_clock_in else '#d97706'};">{status_text}</span></td>
        </tr>
      </table>
      <div style="margin-top: 24px; padding: 14px; background-color: #fefce8; border: 1px solid #fef08a; border-radius: 10px; font-size: 12px; color: #854d0e;">
        <strong>System Information:</strong> Dispatched to verified Admin recipient(s): <em>{', '.join(admin_emails)}</em>.
      </div>
    </div>
    <div class="footer">
      RebongSancon Management System • Group 15 Capstone Project<br>
      Laguna University • Hosted on Firebase Cloud
    </div>
  </div>
</body>
</html>
"""

        msg = MIMEMultipart("alternative")
        msg["From"] = f"RebongSancon Attendance <{sender_email}>"
        msg["To"] = ", ".join(admin_emails)
        msg["Subject"] = subject

        plain_text = (
            f"{action_title}: {employee_name} (@{username}) on {log_date}. "
            f"Time In: {time_in}, Time Out: {time_out}. Status: {status_text}."
        )
        msg.attach(MIMEText(plain_text, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=timeout)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, admin_emails, msg.as_string())
        server.quit()

        print(f"[ATTENDANCE NOTIF SUCCESS] Delivered {action} notification for {username} to {admin_emails}")
        return True, f"Notification sent to {', '.join(admin_emails)}"
    except Exception as e:
        print(f"[ATTENDANCE NOTIF ERROR] Failed to send attendance email: {e}")
        return False, str(e)


def send_attendance_email_async(username, action, attendance_record=None):
    """
    In Cloud Functions / Cloud Run, execute synchronously with a bounded timeout before
    returning the HTTP response so CPU throttling never kills SMTP delivery mid-flight.
    Locally, dispatch on a daemon thread to keep responses snappy.
    """
    if IS_CLOUD:
        try:
            return send_attendance_email(username, action, attendance_record, timeout=8)
        except Exception as e:
            print(f"[ATTENDANCE NOTIF CLOUD WARNING] {e}")
            return False, str(e)

    thread = threading.Thread(
        target=send_attendance_email,
        args=(username, action, attendance_record),
        daemon=True,
    )
    thread.start()
    return thread


def send_test_email(target_email=None):
    """Sends a test attendance alert email to verify system SMTP and Admin recipient configs."""
    try:
        settings = db.get_mail_settings()
        sender_email = (settings.get("sender_email") or "").strip()
        sender_password = (settings.get("sender_password") or "").strip()

        if not sender_email or not sender_password:
            return False, "Sender email or App Password not configured in settings."

        recipients = [target_email] if target_email else db.get_admin_emails()
        if not recipients or not any(recipients):
            return False, "No recipient admin email provided or found in system."

        clean_recipients = [r.strip() for r in recipients if r and "@" in r]
        if not clean_recipients:
            return False, "Recipient email is invalid."

        subject = "🧪 [System Test] RebongSancon Staff Attendance Email System"
        msg = MIMEMultipart("alternative")
        msg["From"] = f"RebongSancon System <{sender_email}>"
        msg["To"] = ", ".join(clean_recipients)
        msg["Subject"] = subject

        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background:#f8fafc; padding:20px;">
          <div style="max-width:550px; margin:0 auto; background:#ffffff; border-radius:12px; border:1px solid #e2e8f0; overflow:hidden;">
            <div style="background:#000000; color:#ffffff; padding:20px; text-align:center; border-bottom:3px solid #eab308;">
              <h2 style="margin:0; color:#eab308;">RebongSancon Funeral Services</h2>
              <p style="margin:4px 0 0 0; font-size:12px; color:#cbd5e1;">Email Notification Test Verification</p>
            </div>
            <div style="padding:24px;">
              <p style="font-size:14px; color:#334155;">Hello Administrator,</p>
              <p style="font-size:13px; color:#475569; line-height:1.6;">
                This test confirmation verifies that your Firebase Cloud email notification service is properly configured!
              </p>
              <div style="background:#f1f5f9; padding:12px 16px; border-radius:8px; font-size:12px; margin:16px 0;">
                <strong>Sender:</strong> {sender_email}<br>
                <strong>Admin Recipient(s):</strong> {', '.join(clean_recipients)}<br>
                <strong>Timestamp (PHT):</strong> {db.now_pht().strftime("%b %d, %Y • %I:%M:%S %p")}
              </div>
              <p style="font-size:12px; color:#10b981; font-weight:bold;">✔ System email integration is operating normally.</p>
            </div>
            <div style="background:#f8fafc; border-top:1px solid #e2e8f0; padding:12px; text-align:center; font-size:11px; color:#94a3b8;">
              RebongSancon Management System • Group 15 Capstone
            </div>
          </div>
        </body>
        </html>
        """
        msg.attach(MIMEText("RebongSancon Attendance Test Email - Email notification service is active.", "plain"))
        msg.attach(MIMEText(html, "html"))

        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=10)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, clean_recipients, msg.as_string())
        server.quit()

        return True, f"Test email successfully delivered to {', '.join(clean_recipients)}"
    except Exception as e:
        return False, str(e)
