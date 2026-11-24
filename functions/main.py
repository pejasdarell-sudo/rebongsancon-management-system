"""
Firebase Cloud Functions (2nd Gen) entrypoint for Rebong-Sancon Management System.
Serves the Flask WSGI application behind Firebase Hosting in asia-southeast1.
"""
from firebase_functions import https_fn, options
from app import app as flask_app


@https_fn.on_request(
    region="asia-southeast1",
    memory=options.MemoryOption.MB_512,
    timeout_sec=60,
    max_instances=10,
    secrets=["FLASK_SECRET_KEY", "GMAIL_USER", "GMAIL_APP_PASSWORD", "MOBILE_API_KEY"],
)
def rebongsancon_web(req: https_fn.Request) -> https_fn.Response:
    """Delegate incoming Firebase Hosting requests through the full Flask WSGI pipeline."""
    return https_fn.Response.from_app(flask_app.wsgi_app, req.environ)
