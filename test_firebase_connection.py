
import os
from pathlib import Path

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, firestore

# Load settings from the local .env file
load_dotenv()

credentials_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
collection_name = os.getenv("FIREBASE_COLLECTION")

if not credentials_path or not collection_name:
    raise ValueError("Required settings are missing from .env")

key_file = Path(credentials_path)

if not key_file.is_absolute():
    key_file = Path(__file__).resolve().parent / key_file

if not key_file.is_file():
    raise FileNotFoundError(
        f"Service-account file not found: {key_file}"
    )

# Initialize Firebase Admin SDK
cred = credentials.Certificate(str(key_file))
firebase_admin.initialize_app(cred)

# Connect to Cloud Firestore
db = firestore.client()

# Read the collection to verify connectivity
list(db.collection(collection_name).limit(1).stream())

print("Firebase connection successful!")
print(f"Firestore collection: {collection_name}")
