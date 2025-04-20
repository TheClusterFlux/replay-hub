import os

# MongoDB Configuration
IS_LOCAL = os.getenv("IS_LOCAL", "false").lower() == "true"

if IS_LOCAL:
    MONGO_URI = "mongodb://localhost:27017"
else:
    MONGO_SERVICE_NAME = os.getenv("MONGO_SERVICE_NAME", "mongodb")
    MONGO_NAMESPACE = os.getenv("MONGO_NAMESPACE", "default")
    MONGO_PORT = int(os.getenv("MONGO_PORT", 27017))
    MONGO_USERNAME = os.getenv("MONGO_USERNAME", "root")
    MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
    MONGO_URI = f"mongodb://{MONGO_USERNAME}:{MONGO_PASSWORD}@{MONGO_SERVICE_NAME}.{MONGO_NAMESPACE}.svc.cluster.local:{MONGO_PORT}"

UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "./uploads")