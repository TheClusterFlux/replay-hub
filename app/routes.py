import os
import threading
from flask import request, jsonify, current_app
from bson.objectid import ObjectId
from app import app, logger
from app.database import save_to_db, fetch_from_db, fs, delete_from_db, update_db, get_single_document
from app.utils import extract_video_metadata, schedule_delete, process_video_for_web_compatibility
from app.config import UPLOAD_FOLDER
from app.s3 import upload_to_s3
import uuid
import requests
import datetime
import tempfile
import shutil
import random
import string

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Configure Flask for large file uploads (10GB max)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB in bytes

# Set streaming threshold to handle large files without loading them into memory
app.config['MAX_CONTENT_LENGTH_FOR_STREAMING'] = 1 * 1024 * 1024  # Use streaming for files larger than 1MB


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
        # Transform the response to match the expected format in the API docs
        formatted_metadata = []
        for item in metadata:
            formatted_item = {
                "id": item.get("_id", str(uuid.uuid4())),
                "short_id": item.get("short_id", ""),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "s3_url": item.get("s3_url", ""),
                "thumbnail_id": item.get("thumbnail_id", ""),
                "duration": item.get("duration", 0),
                "resolution": item.get("resolution", ""),
                "upload_date": item.get("upload_date", datetime.datetime.now().isoformat()),
                "uploader": item.get("uploader", "Anonymous"),
                "views": item.get("views", 0),
                "likes": item.get("likes", 0),
                "dislikes": item.get("dislikes", 0),
                "players": item.get("players", [])
            }
            formatted_metadata.append(formatted_item)
        logger.info(f"Fetched metadata: {formatted_metadata}")
        return jsonify(formatted_metadata), 200
    except Exception as e:
        logger.error(f"Error fetching metadata: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/metadata/<video_id>', methods=['GET'])
def get_video_metadata(video_id):
    """Retrieve metadata for a specific video by _id, short_id, or legacy UUID."""
    try:
        # Try to find by _id first
        video = get_single_document({"_id": video_id})
        if not video:
            # Try to find by short_id
            video = get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"error": "Video not found"}), 404
        # Format the response to match API specs
        formatted_video = {
            "id": video.get("_id", video_id),
            "short_id": video.get("short_id", ""),
            "title": video.get("title", ""),
            "description": video.get("description", ""),
            "s3_url": video.get("s3_url", ""),
            "thumbnail_id": video.get("thumbnail_id", ""),
            "duration": video.get("duration", 0),
            "resolution": video.get("resolution", ""),
            "upload_date": video.get("upload_date", datetime.datetime.now().isoformat()),
            "uploader": video.get("uploader", "Anonymous"),
            "views": video.get("views", 0),
            "likes": video.get("likes", 0),
            "dislikes": video.get("dislikes", 0),
            "players": video.get("players", [])
        }
        return jsonify(formatted_video), 200
    except Exception as e:
        logger.error(f"Error fetching video metadata: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/metadata/<video_id>/view', methods=['POST'])
def increment_view_count(video_id):
    """Increment view count for a specific video by _id or short_id."""
    try:
        # Try to find by _id (ObjectId or string)
        video = get_single_document({"_id": video_id})
        if not video:
            # Try to find by short_id
            video = get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"success": False, "error": "Video not found"}), 404
        
        # Use the actual _id from the found video for updating
        actual_id = video.get("_id")
        if not actual_id:
            return jsonify({"success": False, "error": "Video has no _id"}), 500
            
        update_db({"_id": actual_id}, {"$inc": {"views": 1}})
        
        # Get updated view count
        updated_video = get_single_document({"_id": actual_id})
        current_views = updated_video.get("views", 0) if updated_video else 0
        
        return jsonify({
            "success": True,
            "videoId": actual_id,
            "views": current_views
        }), 200
    except Exception as e:
        logger.error(f"Error incrementing view count: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    

@app.route('/metadata', methods=['DELETE'])
def delete_metadata():
    """Delete metadata from MongoDB with optional filters."""
    try:
        filters = request.args.to_dict()
        deleted_count = delete_from_db(filters)
        return jsonify({"message": f"Deleted {deleted_count} documents"}), 200
    except Exception as e:
        logger.error(f"Error deleting metadata: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload and save a video file."""
    try:
        save_to_s3 = True  
        if 'url' in request.form:
            file_path, internal_name = process_url_request(request.form['url'])
            file = None  # No file object for URL-based uploads
        else:
            # Validate and process the uploaded file
            file, file_path, internal_name, save_to_s3 = process_upload_request()

        # Process video for web compatibility (convert H.265 to H.264 if needed)
        logger.info(f"Processing video for web compatibility: {file_path}")
        processed_file_path = process_video_for_web_compatibility(file_path)
        
        # Update the file path if conversion occurred
        if processed_file_path != file_path:
            logger.info(f"Video converted from H.265 to H.264: {processed_file_path}")
            file_path = processed_file_path

        # Handle S3 or local saving (using the processed file)
        s3_url = handle_file_storage(file, file_path, save_to_s3)

        # Extract video metadata
        video_metadata = extract_video_metadata(file_path)

        # Save the thumbnail to GridFS and delete it locally
        thumbnail_id = save_thumbnail_to_gridfs(video_metadata, internal_name)

        # Combine metadata and save to MongoDB
        combined_metadata = combine_and_save_metadata(
            video_metadata, request.form.to_dict(), internal_name, thumbnail_id, s3_url
        )
        
        # Now that all processing is done, schedule the local file for deletion
        if save_to_s3 and os.path.exists(file_path):
            logger.info(f"Processing complete, scheduling deletion of local file: {file_path}")
            schedule_delete(file_path, delay=3600)  # Delete after 1 hour
        
        # Format response to match API documentation
        response_data = {
            "success": True,
            "metadata": {
                "id": combined_metadata.get("_id", ""),
                "title": combined_metadata.get("title", ""),
                "description": combined_metadata.get("description", ""),
                "s3_url": combined_metadata.get("s3_url", ""),
                "thumbnail_id": combined_metadata.get("thumbnail_id", ""),
                "duration": combined_metadata.get("duration", 0),
                "resolution": combined_metadata.get("resolution", ""),
                "upload_date": combined_metadata.get("upload_date", datetime.datetime.now().isoformat()),
                "uploader": combined_metadata.get("uploader", "Anonymous")
            }
        }

        return jsonify(response_data), 201
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500
    
# Comments API endpoints
@app.route('/comments/<video_id>', methods=['GET'])
def get_comments(video_id):
    """Get all comments for a specific video."""
    try:
        from app.database import comments_collection
        
        # Get top-level comments
        top_comments = list(comments_collection.find({"videoId": video_id, "parent_comment_id": None}))
        
        # Process each comment to include replies
        result = []
        for comment in top_comments:
            comment_id = str(comment.get("_id"))
            
            # Convert ObjectId to string for JSON serialization
            comment["_id"] = comment_id
            
            # Get replies for this comment
            replies = list(comments_collection.find({"parent_comment_id": comment_id}))
            for reply in replies:
                reply["_id"] = str(reply.get("_id"))
            
            # Format the comment according to API docs
            formatted_comment = {
                "id": comment_id,
                "videoId": comment.get("videoId"),
                "userId": comment.get("userId"),
                "username": comment.get("username"),
                "text": comment.get("text"),
                "timestamp": comment.get("timestamp"),
                "likes": comment.get("likes", 0),
                "dislikes": comment.get("dislikes", 0),
                "replies": [{
                    "id": str(reply.get("_id")),
                    "userId": reply.get("userId"),
                    "username": reply.get("username"),
                    "text": reply.get("text"),
                    "timestamp": reply.get("timestamp"),
                    "likes": reply.get("likes", 0),
                    "dislikes": reply.get("dislikes", 0)
                } for reply in replies]
            }
            
            result.append(formatted_comment)
            
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Error fetching comments: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/comments', methods=['POST'])
def add_comment():
    """Add a new comment."""
    try:
        from app.database import comments_collection
        
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["videoId", "userId", "username", "text"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        # Create comment object with default values
        comment = {
            "videoId": data.get("videoId"),
            "userId": data.get("userId"),
            "username": data.get("username"),
            "text": data.get("text"),
            "timestamp": data.get("timestamp", datetime.datetime.now().isoformat()),
            "likes": 0,
            "dislikes": 0,
            "parent_comment_id": None  # This is a top-level comment
        }
        
        # Insert into database
        result = comments_collection.insert_one(comment)
        comment_id = str(result.inserted_id)
        
        # Format response
        response = {
            "id": comment_id,
            "videoId": comment.get("videoId"),
            "userId": comment.get("userId"),
            "username": comment.get("username"),
            "text": comment.get("text"),
            "timestamp": comment.get("timestamp"),
            "likes": 0,
            "dislikes": 0
        }
        
        return jsonify(response), 201
    except Exception as e:
        logger.error(f"Error adding comment: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/comments/<comment_id>/reply', methods=['POST'])
def add_reply(comment_id):
    """Add a reply to a comment."""
    try:
        from app.database import comments_collection
        
        # Check if parent comment exists
        parent = comments_collection.find_one({"_id": ObjectId(comment_id)})
        if not parent:
            return jsonify({"success": False, "error": "Parent comment not found", "code": 404}), 404
            
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["videoId", "userId", "username", "text"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        # Create reply object
        reply = {
            "videoId": data.get("videoId"),
            "userId": data.get("userId"),
            "username": data.get("username"),
            "text": data.get("text"),
            "timestamp": data.get("timestamp", datetime.datetime.now().isoformat()),
            "likes": 0,
            "dislikes": 0,
            "parent_comment_id": comment_id  # Link to parent comment
        }
        
        # Insert into database
        result = comments_collection.insert_one(reply)
        reply_id = str(result.inserted_id)
        
        # Format response
        response = {
            "id": reply_id,
            "videoId": reply.get("videoId"),
            "userId": reply.get("userId"),
            "username": reply.get("username"),
            "text": reply.get("text"),
            "timestamp": reply.get("timestamp"),
            "likes": 0,
            "dislikes": 0
        }
        
        return jsonify(response), 201
    except Exception as e:
        logger.error(f"Error adding reply: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

# Reactions API endpoints
@app.route('/reactions', methods=['POST'])
def add_reaction():
    """Add or update a reaction to a video."""
    try:
        from app.database import reactions_collection
        
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["videoId", "userId", "type"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        video_id = data.get("videoId")
        user_id = data.get("userId")
        reaction_type = data.get("type")
        
        # First check if the video exists before proceeding
        video = get_single_document({"_id": video_id})
        if not video:
            # Try to find by short_id
            video = get_single_document({"short_id": video_id})
            
        if not video:
            logger.error(f"Cannot add reaction: Video with id {video_id} not found")
            return jsonify({"success": False, "error": "Video not found", "code": 404}), 404
            
        # Get the actual _id for database operations
        actual_video_id = video.get("_id")
            
        # Validate reaction type
        if reaction_type not in ["like", "dislike", "none"]:
            return jsonify({"success": False, "error": "Invalid reaction type", "code": 400}), 400
            
        # Check if user already has a reaction
        existing_reaction = reactions_collection.find_one({
            "videoId": video_id,
            "userId": user_id,
            "commentId": None  # This is for a video, not a comment
        })
        
        # Process based on the reaction type
        if reaction_type == "none":
            # Remove reaction if it exists
            if existing_reaction:
                reactions_collection.delete_one({"_id": existing_reaction["_id"]})
                
                # Update video like/dislike counts
                old_type = existing_reaction.get("type")
                if old_type == "like":
                    update_db({"_id": actual_video_id}, {"$inc": {"likes": -1}})
                elif old_type == "dislike":
                    update_db({"_id": actual_video_id}, {"$inc": {"dislikes": -1}})
        else:
            # Add new reaction or update existing
            if existing_reaction:
                old_type = existing_reaction.get("type")
                
                # Only update if the reaction type is changing
                if old_type != reaction_type:
                    reactions_collection.update_one(
                        {"_id": existing_reaction["_id"]},
                        {"$set": {"type": reaction_type, "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())}}
                    )
                    
                    # Update video metrics
                    if old_type == "like":
                        update_db({"_id": actual_video_id}, {"$inc": {"likes": -1}})
                    elif old_type == "dislike":
                        update_db({"_id": actual_video_id}, {"$inc": {"dislikes": -1}})
                        
                    if reaction_type == "like":
                        update_db({"_id": actual_video_id}, {"$inc": {"likes": 1}})
                    elif reaction_type == "dislike":
                        update_db({"_id": actual_video_id}, {"$inc": {"dislikes": 1}})
            else:
                # Create new reaction
                reaction = {
                    "videoId": video_id,
                    "userId": user_id,
                    "type": reaction_type,
                    "commentId": None,
                    "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())
                }
                reactions_collection.insert_one(reaction)
                
                # Update video metrics
                if reaction_type == "like":
                    update_db({"_id": actual_video_id}, {"$inc": {"likes": 1}})
                elif reaction_type == "dislike":
                    update_db({"_id": actual_video_id}, {"$inc": {"dislikes": 1}})
        
        # Get current likes/dislikes count for the video
        updated_video = get_single_document({"_id": actual_video_id})
        
        if not updated_video:
            logger.error(f"Video not found after updating: {video_id}")
            # Since we already verified the video exists above, we'll use the original video object
            # to avoid a 500 error, even if it has outdated counts
            current_likes = video.get("likes", 0)
            current_dislikes = video.get("dislikes", 0)
        else:
            current_likes = updated_video.get("likes", 0)
            current_dislikes = updated_video.get("dislikes", 0)
        
        return jsonify({
            "success": True,
            "videoId": video_id,
            "currentLikes": current_likes,
            "currentDislikes": current_dislikes
        }, 200)
    except Exception as e:
        logger.error(f"Error adding reaction: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/comments/<comment_id>/reactions', methods=['POST'])
def add_comment_reaction(comment_id):
    """Add or update a reaction to a comment."""
    try:
        from app.database import reactions_collection, comments_collection
        
        # Check if comment exists
        comment = comments_collection.find_one({"_id": ObjectId(comment_id)})
        if not comment:
            return jsonify({"success": False, "error": "Comment not found", "code": 404}), 404
            
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["userId", "type"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        user_id = data.get("userId")
        reaction_type = data.get("type")
        
        # Validate reaction type
        if reaction_type not in ["like", "dislike", "none"]:
            return jsonify({"success": False, "error": "Invalid reaction type", "code": 400}), 400
            
        # Check if user already has a reaction to this comment
        existing_reaction = reactions_collection.find_one({
            "commentId": comment_id,
            "userId": user_id
        })
        
        # Process based on the reaction type
        if reaction_type == "none":
            # Remove reaction if it exists
            if existing_reaction:
                reactions_collection.delete_one({"_id": existing_reaction["_id"]})
                
                # Update comment like/dislike counts
                old_type = existing_reaction.get("type")
                if old_type == "like":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": -1}})
                elif old_type == "dislike":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": -1}})
        else:
            # Add new reaction or update existing
            if existing_reaction:
                old_type = existing_reaction.get("type")
                
                # Only update if the reaction type is changing
                if old_type != reaction_type:
                    reactions_collection.update_one(
                        {"_id": existing_reaction["_id"]},
                        {"$set": {"type": reaction_type, "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())}}
                    )
                    
                    # Update comment metrics
                    if old_type == "like":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": -1}})
                    elif old_type == "dislike":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": -1}})
                        
                    if reaction_type == "like":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": 1}})
                    elif reaction_type == "dislike":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": 1}})
            else:
                # Create new reaction
                reaction = {
                    "commentId": comment_id,
                    "userId": user_id,
                    "type": reaction_type,
                    "videoId": None,
                    "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())
                }
                reactions_collection.insert_one(reaction)
                
                # Update comment metrics
                if reaction_type == "like":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": 1}})
                elif reaction_type == "dislike":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": 1}})
        
        # Get current likes/dislikes count for the comment
        updated_comment = comments_collection.find_one({"_id": ObjectId(comment_id)})
        current_likes = updated_comment.get("likes", 0)
        current_dislikes = updated_comment.get("dislikes", 0)
        
        return jsonify({
            "success": True,
            "commentId": comment_id,
            "currentLikes": current_likes,
            "currentDislikes": current_dislikes
        }), 200
    except Exception as e:
        logger.error(f"Error adding comment reaction: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/thumbnail/<video_id>', methods=['POST'])
def upload_thumbnail(video_id):
    """Upload a custom thumbnail for a video."""
    try:
        # Check if the video exists by _id or short_id
        video = get_single_document({"_id": video_id})
        if not video:
            video = get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"success": False, "error": "Video not found", "code": 404}), 404
            
        # Get the actual _id for database operations
        actual_video_id = video.get("_id")
            
        # Validate file upload
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file part in the request", "code": 400}), 400
            
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected", "code": 400}), 400
            
        # Save thumbnail to GridFS
        thumbnail_id = fs.put(file, filename=f"{video_id}_custom_thumbnail.jpg")
        
        # Update the video's thumbnail_id using the actual _id
        update_db({"_id": actual_video_id}, {"$set": {"thumbnail_id": str(thumbnail_id)}})
        
        # Generate the thumbnail URL
        thumbnail_url = f"{request.url_root.rstrip('/')}/thumbnail/{str(thumbnail_id)}"
        
        return jsonify({
            "success": True,
            "videoId": actual_video_id,
            "thumbnailId": str(thumbnail_id),
            "url": thumbnail_url
        }), 200
    except Exception as e:
        logger.error(f"Error uploading thumbnail: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

# Keep the existing code for process_upload_request, handle_file_storage, etc.

def process_url_request(url):
    """Process a video from a URL.
    
    :param url: URL of the video to download
    :return: tuple of (file_path, internal_name)
    """
    logger.info(f"Processing video from URL: {url}")
    
    # Generate a unique name for the file
    internal_name = str(uuid.uuid4())
    file_ext = url.split('.')[-1].split('?')[0] if '.' in url.split('/')[-1] else 'mp4'
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{internal_name}.{file_ext}")
    
    # Download the file in chunks to handle large files
    try:
        with requests.get(url, stream=True) as response:
            response.raise_for_status()
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info(f"Successfully downloaded video from URL to: {file_path}")
        return file_path, internal_name
    except Exception as e:
        logger.error(f"Error downloading video from URL: {e}")
        raise

def process_upload_request():
    """Process an uploaded file from a multipart/form-data request.
    
    :return: tuple of (file, file_path, internal_name, save_to_s3)
    """
    logger.info("Processing file upload request")
    
    # Check if the post request has the file part
    if 'file' not in request.files:
        logger.error("No file part in the request")
        raise ValueError("No file part in the request")
        
    file = request.files['file']
    
    # If user does not select file, browser may submit an empty file without filename
    if file.filename == '':
        logger.error("No file selected for uploading")
        raise ValueError("No file selected for uploading")
        
    # Generate a unique name for the file
    internal_name = str(uuid.uuid4())
    file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'mp4'
    filename = f"{internal_name}.{file_ext}"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    
    # Check for streaming threshold
    content_length = request.content_length or 0
    if content_length > app.config.get('MAX_CONTENT_LENGTH_FOR_STREAMING', 1024 * 1024):
        logger.info(f"File size exceeds streaming threshold ({content_length} bytes). Using streaming upload.")
        
        # Stream the file to disk in chunks
        try:
            file.save(file_path)
            logger.info(f"Successfully saved large file to: {file_path}")
        except Exception as e:
            logger.error(f"Error saving large file: {e}")
            raise
    else:
        # For smaller files, use the standard approach
        try:
            file.save(file_path)
            logger.info(f"Successfully saved file to: {file_path}")
        except Exception as e:
            logger.error(f"Error saving file: {e}")
            raise
            
    # Determine if we should save to S3
    save_to_s3 = request.form.get('save_to_s3', 'true').lower() != 'false'
    
    return file, file_path, internal_name, save_to_s3

def handle_file_storage(file, file_path, save_to_s3=True):
    """Handle S3 or local storage for an uploaded file.
    
    :param file: The file object (may be None for URL-based uploads)
    :param file_path: Path where the file is temporarily stored
    :param save_to_s3: Whether to save to S3 (True) or keep locally (False)
    :return: URL where the file is accessible
    """
    logger.info(f"Handling file storage for: {file_path}, Save to S3: {save_to_s3}")
    
    try:
        if save_to_s3:
            # Determine content type based on file extension
            content_type = None
            if file_path:
                ext = os.path.splitext(file_path)[1].lower()
                if ext == '.mp4':
                    content_type = 'video/mp4'
                elif ext in ['.m3u8', '.m3u']:
                    content_type = 'application/x-mpegURL'
                elif ext == '.ts':
                    content_type = 'video/MP2T'
            
            # Upload to S3 directly from the file path (streaming)
            logger.info(f"Uploading file to S3: {file_path}")
            s3_url = upload_to_s3(file_path, content_type=content_type)
            
            if s3_url:
                logger.info(f"File uploaded successfully to S3. URL: {s3_url}")
                # Don't schedule deletion here - let the upload_file function handle this
                # after processing is complete
                return s3_url
            else:
                logger.error("Failed to upload to S3, falling back to local storage")
        
        # If not saving to S3 or S3 upload failed, return local URL
        filename = os.path.basename(file_path)
        local_url = f"/uploads/{filename}"
        logger.info(f"Using local storage. URL: {local_url}")
        return local_url
        
    except Exception as e:
        logger.error(f"Error handling file storage: {e}")
        raise

def save_thumbnail_to_gridfs(video_metadata, internal_name):
    """Save thumbnail to GridFS and return its ID.
    
    :param video_metadata: Metadata dict that contains the thumbnail path
    :param internal_name: Internal name of the video
    :return: ObjectId of the saved thumbnail
    """
    logger.info("Saving thumbnail to GridFS")
    
    thumbnail_path = video_metadata.get('thumbnail_path')
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        logger.warning("No thumbnail available")
        return None
        
    try:
        with open(thumbnail_path, 'rb') as f:
            thumbnail_id = fs.put(f, filename=f"{internal_name}_thumbnail.jpg")
        logger.info(f"Thumbnail saved to GridFS with ID: {thumbnail_id}")
        
        # Delete local thumbnail file as it's now in GridFS
        os.remove(thumbnail_path)
        logger.info(f"Local thumbnail file deleted: {thumbnail_path}")
        
        return str(thumbnail_id)
    except Exception as e:
        logger.error(f"Error saving thumbnail to GridFS: {e}")
        return None

def combine_and_save_metadata(video_metadata, form_data, internal_name, thumbnail_id, s3_url):
    """Combine metadata from various sources and save to the database.
    
    :param video_metadata: Metadata extracted from the video file
    :param form_data: Metadata from the form submission
    :param internal_name: Internal name of the video
    :param thumbnail_id: ID of the thumbnail in GridFS
    :param s3_url: URL of the video in S3 or local storage
    :return: Combined metadata dict as saved to the database
    """
    logger.info("Combining and saving metadata")
    
    # Process players if provided
    players = []
    if form_data.get('players'):
        try:
            import json
            players = json.loads(form_data.get('players', '[]'))
        except Exception as e:
            logger.error(f"Error parsing players JSON: {e}")
    
    # Generate or use existing short_id
    short_id = form_data.get('short_id') or generate_short_id()
    
    # Create the metadata document
    metadata = {
        "_id": form_data.get('id', str(uuid.uuid4())),
        "short_id": short_id,
        "title": form_data.get('title', os.path.basename(video_metadata.get('file_path', ''))),
        "description": form_data.get('description', ''),
        "s3_url": s3_url,
        "internal_name": internal_name,
        "thumbnail_id": thumbnail_id,
        "duration": video_metadata.get('duration', 0),
        "resolution": video_metadata.get('resolution', ''),
        "upload_date": datetime.datetime.now().isoformat(),
        "uploader": form_data.get('uploader', 'Anonymous'),
        "views": 0,
        "likes": 0,
        "dislikes": 0,
        "players": players
    }
    
    # Save to database
    save_to_db(metadata)
    logger.info(f"Metadata saved to database with ID: {metadata['_id']}")
    
    return metadata

def generate_short_id(length=8):
    """Generate a short, unique, URL-safe ID."""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

@app.route('/upload/init', methods=['POST'])
def init_chunked_upload():
    """Initialize a chunked upload session."""
    try:
        logger.info("Initializing chunked upload")
        
        if 'action' not in request.form or request.form.get('action') != 'init_chunked_upload':
            logger.error("Invalid action for chunked upload initialization")
            return jsonify({"success": False, "error": "Invalid action", "code": 400}), 400
            
        file_id = request.form.get('fileId')
        if not file_id:
            logger.error("No fileId provided for chunked upload")
            return jsonify({"success": False, "error": "No fileId provided", "code": 400}), 400
            
        # Create a temporary directory for this upload
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], f"chunks_{file_id}")
        os.makedirs(upload_dir, exist_ok=True)
        
        # Store basic metadata for this upload
        metadata = {
            "fileId": file_id,
            "filename": request.form.get('filename', ''),
            "fileSize": request.form.get('fileSize', '0'),
            "totalChunks": request.form.get('totalChunks', '0'),
            "chunksReceived": 0,
            "title": request.form.get('title', ''),
            "description": request.form.get('description', ''),
            "uploader": request.form.get('uploader', 'Anonymous'),
            "players": request.form.get('players', '[]'),
            "status": "initialized",
            "created_at": datetime.datetime.now().isoformat()
        }
        
        # Save metadata to a JSON file
        with open(os.path.join(upload_dir, "metadata.json"), 'w') as f:
            import json
            json.dump(metadata, f)
            
        logger.info(f"Chunked upload initialized with ID: {file_id}")
        
        return jsonify({
            "success": True,
            "fileId": file_id,
            "message": "Chunked upload initialized successfully"
        }), 200
        
    except Exception as e:
        logger.error(f"Error initializing chunked upload: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500
        
@app.route('/upload/chunk', methods=['POST'])
def upload_chunk():
    """Handle uploading individual chunks of a file."""
    try:
        logger.info("Processing chunk upload")
        
        # Validate request
        if 'file' not in request.files:
            logger.error("No file part in chunk upload request")
            return jsonify({"success": False, "error": "No file part in the request", "code": 400}), 400
            
        file = request.files['file']
        if file.filename == '':
            logger.error("No file selected for chunk upload")
            return jsonify({"success": False, "error": "No file selected", "code": 400}), 400
            
        file_id = request.form.get('fileId')
        chunk_index = request.form.get('chunkIndex')
        total_chunks = request.form.get('totalChunks')
        
        if not file_id or not chunk_index or not total_chunks:
            logger.error("Missing required parameters for chunk upload")
            return jsonify({"success": False, "error": "Missing required parameters", "code": 400}), 400
            
        # Check if the upload was initialized
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], f"chunks_{file_id}")
        if not os.path.exists(upload_dir):
            logger.error(f"Upload directory not found for fileId: {file_id}")
            return jsonify({"success": False, "error": "Upload not initialized", "code": 400}), 400
            
        # Save this chunk to the upload directory
        chunk_path = os.path.join(upload_dir, f"chunk_{chunk_index}")
        file.save(chunk_path)
        
        # Update metadata
        import json
        metadata_path = os.path.join(upload_dir, "metadata.json")
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
            
        metadata["chunksReceived"] = metadata.get("chunksReceived", 0) + 1
        
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f)
            
        logger.info(f"Chunk {chunk_index} of {total_chunks} saved for file ID {file_id}")
        
        return jsonify({
            "success": True,
            "fileId": file_id,
            "chunkIndex": chunk_index,
            "chunksReceived": metadata["chunksReceived"],
            "totalChunks": total_chunks
        }), 200
        
    except Exception as e:
        logger.error(f"Error uploading chunk: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500
        
@app.route('/upload/finalize', methods=['POST'])
def finalize_upload():
    """Finalize a chunked upload by combining all chunks and processing the complete file."""
    try:
        logger.info("Finalizing chunked upload")
        
        if 'action' not in request.form or request.form.get('action') != 'finalize_chunked_upload':
            logger.error("Invalid action for chunked upload finalization")
            return jsonify({"success": False, "error": "Invalid action", "code": 400}), 400
            
        file_id = request.form.get('fileId')
        if not file_id:
            logger.error("No fileId provided for chunked upload finalization")
            return jsonify({"success": False, "error": "No fileId provided", "code": 400}), 400
            
        # Check if the upload was initialized
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], f"chunks_{file_id}")
        if not os.path.exists(upload_dir):
            logger.error(f"Upload directory not found for fileId: {file_id}")
            return jsonify({"success": False, "error": "Upload not initialized", "code": 400}), 400
            
        # Load metadata
        import json
        metadata_path = os.path.join(upload_dir, "metadata.json")
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
            
        # Check if all chunks have been received
        chunks_received = metadata.get("chunksReceived", 0)
        total_chunks = int(metadata.get("totalChunks", "0"))
        
        if chunks_received != total_chunks:
            logger.error(f"Not all chunks received. Got {chunks_received}/{total_chunks}")
            return jsonify({
                "success": False,
                "error": f"Not all chunks received. Got {chunks_received}/{total_chunks}",
                "code": 400
            }), 400
            
        # Generate a name for the complete file
        original_filename = request.form.get('filename', metadata.get("filename", ""))
        file_ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'mp4'
        internal_name = str(uuid.uuid4())
        complete_file_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{internal_name}.{file_ext}")
        
        # Combine all chunks into the complete file
        with open(complete_file_path, 'wb') as outfile:
            for i in range(total_chunks):
                chunk_path = os.path.join(upload_dir, f"chunk_{i}")
                
                if not os.path.exists(chunk_path):
                    logger.error(f"Chunk {i} not found at {chunk_path}")
                    return jsonify({"success": False, "error": f"Chunk {i} not found", "code": 400}), 400
                
                with open(chunk_path, 'rb') as infile:
                    outfile.write(infile.read())
                    
        logger.info(f"All chunks combined into complete file: {complete_file_path}")
        
        # Update metadata status
        metadata["status"] = "combined"
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f)
            
        # Now process the complete file similar to the regular upload endpoint
        save_to_s3 = True
        
        # Process video for web compatibility (convert H.265 to H.264 if needed)
        logger.info(f"Processing chunked video for web compatibility: {complete_file_path}")
        processed_file_path = process_video_for_web_compatibility(complete_file_path)
        
        # Update the file path if conversion occurred
        if processed_file_path != complete_file_path:
            logger.info(f"Chunked video converted from H.265 to H.264: {processed_file_path}")
            complete_file_path = processed_file_path
        
        # Handle S3 or local saving
        s3_url = handle_file_storage(None, complete_file_path, save_to_s3)
        
        # Extract video metadata
        video_metadata = extract_video_metadata(complete_file_path)

        # Save the thumbnail to GridFS and delete it locally
        thumbnail_id = save_thumbnail_to_gridfs(video_metadata, internal_name)
        
        # Get form data from the original metadata
        form_data = {
            "title": request.form.get('title', metadata.get("title", "")),
            "description": request.form.get('description', metadata.get("description", "")),
            "uploader": request.form.get('uploader', metadata.get("uploader", "Anonymous")),
            "players": request.form.get('players', metadata.get("players", "[]"))
        }
        
        # Combine metadata and save to MongoDB
        combined_metadata = combine_and_save_metadata(
            video_metadata, form_data, internal_name, thumbnail_id, s3_url
        )
        
        # Schedule deletion of the complete file now that processing is done
        if save_to_s3 and os.path.exists(complete_file_path):
            logger.info(f"Scheduling deletion of complete file: {complete_file_path}")
            schedule_delete(complete_file_path, delay=3600)  # Delete after 1 hour
            
        # Schedule deletion of the chunks directory
        logger.info(f"Scheduling deletion of chunks directory: {upload_dir}")
        schedule_delete(upload_dir, delay=3600)  # Delete after 1 hour
        
        # Format response to match API documentation
        response_data = {
            "success": True,
            "metadata": {
                "id": combined_metadata.get("_id", ""),
                "title": combined_metadata.get("title", ""),
                "description": combined_metadata.get("description", ""),
                "s3_url": combined_metadata.get("s3_url", ""),
                "thumbnail_id": combined_metadata.get("thumbnail_id", ""),
                "duration": combined_metadata.get("duration", 0),
                "resolution": combined_metadata.get("resolution", ""),
                "upload_date": combined_metadata.get("upload_date", datetime.datetime.now().isoformat()),
                "uploader": combined_metadata.get("uploader", "Anonymous")
            }
        }
        
        return jsonify(response_data), 201
        
    except Exception as e:
        logger.error(f"Error finalizing chunked upload: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500