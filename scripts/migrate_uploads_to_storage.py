"""
Migrate existing local casket & promo images from:
    Rebong Sancon Funeral Service/static/uploads/caskets/
    Rebong Sancon Funeral Service/static/uploads/promos/
into Firebase Storage, and update matching Firestore documents (`caskets` and `settings/billboard`)
so every image URL resolves persistently in Firebase Cloud.

Usage:
    python scripts/migrate_uploads_to_storage.py
    python scripts/migrate_uploads_to_storage.py --dry-run
"""
import argparse
import io
import mimetypes
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FUNCTIONS_DIR = os.path.join(ROOT_DIR, "functions")
LEGACY_UPLOADS_DIR = os.path.join(ROOT_DIR, "Rebong Sancon Funeral Service", "static", "uploads")

if FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, FUNCTIONS_DIR)

import db


class _FileBytesWrapper(io.BytesIO):
    def __init__(self, data, mimetype):
        super().__init__(data)
        self.mimetype = mimetype


def upload_local_file(local_path, folder):
    filename = os.path.basename(local_path)
    mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    with open(local_path, "rb") as f:
        wrapper = _FileBytesWrapper(f.read(), mime)
    return db.upload_image_to_storage(wrapper, folder=folder, unique_name=filename)


def migrate(dry_run=False):
    url_map = {}  # legacy relative path -> new Firebase Storage HTTPS URL

    for folder in ("caskets", "promos"):
        folder_dir = os.path.join(LEGACY_UPLOADS_DIR, folder)
        if not os.path.isdir(folder_dir):
            continue
        for fname in sorted(os.listdir(folder_dir)):
            fpath = os.path.join(folder_dir, fname)
            if not os.path.isfile(fpath):
                continue
            rel_key = f"/static/uploads/{folder}/{fname}"
            if dry_run:
                print(f"[DRY-RUN] Would upload {rel_key} -> uploads/{folder}/{fname}")
                url_map[rel_key] = f"<firebase-storage-url-for-{fname}>"
            else:
                new_url = upload_local_file(fpath, folder)
                if new_url:
                    print(f"[UPLOADED] {rel_key} -> {new_url}")
                    url_map[rel_key] = new_url
                else:
                    print(f"[ERROR] Failed to upload {rel_key}")

    # Update Firestore caskets docs referencing local /static/uploads/caskets/...
    updated_caskets = 0
    for doc in db.db.collection("caskets").stream():
        data = doc.to_dict() or {}
        img_url = str(data.get("image_url") or "")
        qr_url = str(data.get("qr_image_url") or "")
        updates = {}

        if img_url in url_map:
            updates["image_url"] = url_map[img_url]
        if qr_url.startswith("/static/uploads/qrcodes/"):
            code = data.get("casket_code") or doc.id
            updates["qr_image_url"] = f"/api/qr/{code}.png"

        if updates:
            if dry_run:
                print(f"[DRY-RUN] Would update caskets/{doc.id}: {updates}")
            else:
                db.db.collection("caskets").document(doc.id).update(updates)
                print(f"[UPDATED] caskets/{doc.id}: {list(updates.keys())}")
            updated_caskets += 1

    # Update billboard setting if referencing local upload
    b_ref = db.db.collection("settings").document("billboard")
    b_doc = b_ref.get()
    if b_doc.exists:
        b_url = str((b_doc.to_dict() or {}).get("image_url") or "")
        if b_url in url_map:
            if dry_run:
                print(f"[DRY-RUN] Would update settings/billboard image_url -> {url_map[b_url]}")
            else:
                b_ref.update({"image_url": url_map[b_url]})
                print(f"[UPDATED] settings/billboard -> {url_map[b_url]}")

    print(f"[DONE] Processed {len(url_map)} media file(s) and {updated_caskets} casket record(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate legacy local uploads to Firebase Storage.")
    parser.add_argument("--dry-run", action="store_true", help="Preview uploads and Firestore updates without writing.")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
