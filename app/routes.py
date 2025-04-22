import os
import threading
from flask import request, jsonify
from bson.objectid import ObjectId
from app import app, logger
from app.database import save_to_db, fetch_from_db, fs, delete_from_db
from app.utils import extract_video_metadata, schedule_delete
from app.config import UPLOAD_FOLDER
import uuid
import requests  

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


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
        return jsonify(metadata), 200
    except Exception as e:
        logger.error(f"Error fetching metadata: {e}")
        return jsonify({"error": str(e)}), 500
    

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
        # Check if a URL is provided
        if 'url' in request.form:
            file_path, internal_name = process_url_request(request.form['url'])
            save_to_s3 = False  # URL-based uploads are always saved locally
            file = None  # No file object for URL-based uploads
        else:
            # Validate and process the uploaded file
            file, file_path, internal_name, save_to_s3 = process_upload_request()

        # Handle S3 or local saving
        s3_url = handle_file_storage(file, file_path, save_to_s3)

        # Extract video metadata
        video_metadata = extract_video_metadata(file_path)

        # Save the thumbnail to GridFS and delete it locally
        thumbnail_id = save_thumbnail_to_gridfs(video_metadata, internal_name)

        # Combine metadata and save to MongoDB
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
    finally:
        # Ensure the file is closed if it was opened
        if 'file' in locals() and file:
            file.close()
    
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
        
        if file:  # Save the file locally only if it exists
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

    
    
def process_url_request(url):
    """Download a file from a URL and return its local path and internal name."""
    try:
        logger.info(f"Downloading file from URL: {url}")
        
        # Special handling for Steam CDN URLs
        if "cdn.steamusercontent.com" in url:
            return process_steam_cdn_url(url)
        
        # Standard download for other URLs
        response = requests.get(url, stream=True)
        response.raise_for_status()  # Raise an error for HTTP issues

        # Generate a unique internal name
        internal_name = str(uuid.uuid4())
        file_extension = os.path.splitext(url.split('?')[0])[-1] or ".mp4"  # Default to .mp4 if no extension
        filename = f"{internal_name}{file_extension}"
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

        # Save the file locally
        with open(file_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)

        logger.info(f"File downloaded and saved locally at: {file_path}")
        return file_path, internal_name
    except Exception as e:
        logger.error(f"Error downloading file from URL: {e}")
        raise ValueError(f"Failed to download file from the provided URL: {e}")

def process_steam_cdn_url(url):
    """Handle Steam CDN URLs that require embedding."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        import time
        
        # Generate a unique internal name
        internal_name = str(uuid.uuid4())
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{internal_name}.mp4")
        
        # Create a simple HTML file that embeds the steam content
        embed_html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Steam Content Embed</title></head>
        <body>
            <video width="640" height="480" controls autoplay id="steamVideo">
                <source src="{url}" type="video/mp4">
                Your browser does not support the video tag.
            </video>
            <script>
                // Mark when video is loaded
                document.getElementById('steamVideo').onloadeddata = function() {{
                    document.title = "LOADED:" + document.title;
                }};
            </script>
        </body>
        </html>
        """
        
        embed_path = os.path.join(app.config["UPLOAD_FOLDER"], f"embed_{internal_name}.html")
        with open(embed_path, 'w') as f:
            f.write(embed_html)
            
        logger.info(f"Created embed HTML at: {embed_path}")
        
        # Setup headless browser
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        
        driver = webdriver.Chrome(options=chrome_options)
        
        try:
            # Load the embed page
            driver.get(f"file://{os.path.abspath(embed_path)}")
            logger.info("Waiting for video to load in headless browser...")
            
            # Wait for video to load (up to 30 seconds)
            max_wait = 30
            for _ in range(max_wait):
                if "LOADED:" in driver.title:
                    break
                time.sleep(1)
                
            # Get the video content using JavaScript
            video_src = driver.execute_script("""
                var video = document.querySelector('video');
                return video.src;
            """)
            
            logger.info(f"Extracted video source: {video_src}")
            
            # Download the video using requests
            response = requests.get(video_src, stream=True)
            response.raise_for_status()
            
            with open(file_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
                    
            logger.info(f"Successfully saved Steam video to: {file_path}")
            
            # Clean up the embed file
            os.remove(embed_path)
            
            return file_path, internal_name
        finally:
            driver.quit()
            
    except ImportError:
        logger.error("Selenium not installed. Cannot process Steam CDN URLs.")
        raise ValueError("Selenium is required to process Steam CDN URLs. Install with 'pip install selenium'")
    except Exception as e:
        logger.error(f"Error processing Steam CDN URL: {e}")
        raise ValueError(f"Failed to process Steam CDN URL: {e}")