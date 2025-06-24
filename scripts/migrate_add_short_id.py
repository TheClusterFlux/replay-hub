"""
Standalone migration script to add a short_id to all video documents in MongoDB that do not already have one.
Run this script ONCE from the backend server environment.
"""
import os
import string
import random
from pymongo import MongoClient

# --- CONFIGURATION ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
IS_LOCAL = os.getenv("IS_LOCAL", "false").lower() == "true"
DB_NAME = "replay_hub"
COLLECTION_NAME = "test_data" if IS_LOCAL else "prod_data"

SHORT_ID_LENGTH = 8

def generate_short_id(length=SHORT_ID_LENGTH):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

def main():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    print(f"Connected to {MONGO_URI}, database: {DB_NAME}, collection: {COLLECTION_NAME}")

    # Find all documents without a short_id
    query = {"short_id": {"$exists": False}}
    docs = list(collection.find(query))
    print(f"Found {len(docs)} documents without short_id.")

    # To avoid collisions, keep a set of all used short_ids
    used_short_ids = set(doc.get("short_id") for doc in collection.find({"short_id": {"$exists": True}}))

    updated = 0
    for doc in docs:
        # Generate a unique short_id
        while True:
            short_id = generate_short_id()
            if short_id not in used_short_ids:
                used_short_ids.add(short_id)
                break
        # Update the document
        result = collection.update_one({"_id": doc["_id"]}, {"$set": {"short_id": short_id}})
        if result.modified_count == 1:
            updated += 1
            print(f"Updated document {_id_str(doc['_id'])} with short_id: {short_id}")
        else:
            print(f"Failed to update document {_id_str(doc['_id'])}")
    print(f"Migration complete. {updated} documents updated.")

def _id_str(_id):
    try:
        return str(_id)
    except Exception:
        return repr(_id)

if __name__ == "__main__":
    main()
