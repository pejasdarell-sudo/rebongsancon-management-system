"""
One-time Firestore seeding & QR metadata sync script.
Run explicitly instead of at Flask/Cloud Function module import time:
    python scripts/seed_firestore.py
"""
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FUNCTIONS_DIR = os.path.join(ROOT_DIR, "functions")
if FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, FUNCTIONS_DIR)

import db


def main():
    print("[SEED] Initializing Firebase Admin SDK and checking Firestore collections...")
    db.seed_initial_data_if_empty()
    db.sync_all_package_qrs()
    print("[SEED] Completed seeding and cloud QR metadata sync.")


if __name__ == "__main__":
    main()
