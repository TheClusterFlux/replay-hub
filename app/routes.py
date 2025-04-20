import os
import threading
from flask import request, jsonify
from bson.objectid import ObjectId
from app import app, logger
from app.database import save_to_db, fetch_from_db, fs
from app.utils import extract_video_metadata, schedule_delete
from app.config import UPLOAD_FOLDER
import uuid

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload and save a video file."""
    try:
        # Validate and process the uploaded file
        file, file_path, internal_name, save_to_s3 = process_upload_request()
        
        s3_url = handle_file_storage(file, file_path, save_to_s3)

        video_metadata = extract_video_metadata(file_path)

        thumbnail_id = save_thumbnail_to_gridfs(video_metadata, internal_name)

        combined_metadata = combine_and_save_metadata(
            video_metadata, request.form.to_dict(), internal_name, thumbnail_id, s3_url
        )

        return jsonify({
            "message": "File uploaded successfully",
            "file_path": file_path if not s3_url else None,
            "s3_url": s3_url,
            "metadata": combined_metadata,
            "metadata_id": combined_metadata["_id"]
        }), 201
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/thumbnail/<thumbnail_id>', methods=['GET'])
def get_thumbnail(thumbnail_id):
    """Retrieve a thumbnail from GridFS."""
    try:
        thumbnail = fs.get(ObjectId(thumbnail_id))
        return app.response_class(thumbnail.read(), mimetype='image/jpeg')
    except Exception as e:
        logger.error(f"Error fetching thumbnail: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/metadata', methods=['GET'])
def get_metadata():
    """Retrieve metadata from MongoDB."""
    try:
        filters = request.args.to_dict()
        metadata = fetch_from_db(filters)
        return jsonify({"metadata": metadata}), 200
    except Exception as e:
        logger.error(f"Error fetching metadata: {e}")
        return jsonify({"error": str(e)}), 500
    
    
    
def process_upload_request():
    """Validate and process the uploaded file."""
    if 'file' not in request.files:
        raise ValueError("No file part in the request")

    file = request.files['file']
    if file.filename == '':
        raise ValueError("No file selected for upload")

    internal_name = str(uuid.uuid4())
    file_extension = os.path.splitext(file.filename)[1]
    filename = f"{internal_name}{file_extension}"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    user_metadata = request.form.to_dict()
    save_to_s3 = user_metadata.get("s3", "false").lower() == "true"

    return file, file_path, internal_name, save_to_s3

def handle_file_storage(file, file_path, save_to_s3):
    """Handle saving the file locally or to S3."""
    if save_to_s3:
        logger.info(f"Mock saving file to S3 with internal name: {file_path}")
        return f"https://mock-s3-bucket/{os.path.basename(file_path)}"
    else:
        file.save(file_path)
        logger.info(f"File uploaded and saved locally at: {file_path}")
        threading.Thread(target=schedule_delete, args=(file_path, 600)).start()
        return None
    
def save_thumbnail_to_gridfs(video_metadata, internal_name):
    """Save the thumbnail to GridFS and delete it locally."""
    thumbnail_name = f"{internal_name}_thumbnail.jpg"
    with open(video_metadata["thumbnail"], "rb") as thumbnail_file:
        thumbnail_id = fs.put(thumbnail_file, filename=thumbnail_name)
        logger.info(f"Thumbnail saved to GridFS with ID: {thumbnail_id}")

    # Delete the thumbnail from local storage
    try:
        os.remove(video_metadata["thumbnail"])
        logger.info(f"Thumbnail deleted from local storage: {video_metadata['thumbnail']}")
    except Exception as e:
        logger.warning(f"Failed to delete thumbnail from local storage: {e}")

    return thumbnail_id


def combine_and_save_metadata(video_metadata, user_metadata, internal_name, thumbnail_id, s3_url):
    """Combine metadata and save it to MongoDB."""
    combined_metadata = {**video_metadata, **user_metadata}
    combined_metadata.update({
        "internal_name": internal_name,
        "thumbnail_id": str(thumbnail_id),
        "s3_url": s3_url
    })

    metadata_id = save_to_db(combined_metadata)
    combined_metadata["_id"] = metadata_id
    logger.info(f"Metadata saved with ID: {metadata_id}")

    return combined_metadata