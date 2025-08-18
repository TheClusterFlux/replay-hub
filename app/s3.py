import os
import boto3
import mimetypes
import time
from botocore.exceptions import ClientError
from app.config import AWS_ACCESS_KEY, AWS_SECRET_KEY, S3_BUCKET_NAME, S3_REGION
import logging
import threading
from boto3.s3.transfer import TransferConfig

# Configure logger
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# S3 Transfer configuration for better performance
MB = 1024 * 1024
transfer_config = TransferConfig(
    multipart_threshold=64 * MB,  # Use multipart for files larger than 64MB
    max_concurrency=10,           # Use up to 10 threads for concurrent uploads
    multipart_chunksize=64 * MB,  # 64MB per chunk
    use_threads=True              # Enable threading
)

def upload_to_s3(file_path, object_name=None, content_type=None):
    """Upload a file to an S3 bucket and return the URL

    :param file_path: File to upload
    :param object_name: S3 object name (if not specified, file_name is used)
    :param content_type: Optional content type override
    :return: Public URL of the uploaded file or None if error
    """
    upload_start_time = time.time()
    logger.info(f"☁️ Starting S3 upload for: {file_path}")

    # If S3 object_name was not specified, use the filename from file_path
    if object_name is None:
        object_name = os.path.basename(file_path)
        logger.info(f"📝 Using file name as object name: {object_name}")

    # Verify that the file exists
    if not os.path.exists(file_path):
        elapsed = time.time() - upload_start_time
        logger.error(f"❌ File does not exist after {elapsed:.3f}s: {file_path}")
        return None

    # Get file size for logging
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / MB
    logger.info(f"📦 File size: {file_size_mb:.2f}MB")

    # Determine the file's content type if not provided
    content_type_start = time.time()
    if content_type is None:
        content_type = get_content_type(file_path)
    content_type_elapsed = time.time() - content_type_start
    logger.info(f"🏷️ Content type detected in {content_type_elapsed:.3f}s: {content_type}")

    # Log AWS configuration details (without exposing sensitive information)
    logger.info(f"🌐 Target: {S3_REGION}/{S3_BUCKET_NAME}")

    # Create a boto3 client
    client_start = time.time()
    try:
        logger.info("🔧 Creating S3 client...")
        s3_client = boto3.client(
            's3',
            region_name=S3_REGION,
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY
        )
        client_elapsed = time.time() - client_start
        logger.info(f"✅ S3 client created in {client_elapsed:.3f}s")
    except Exception as e:
        elapsed = time.time() - upload_start_time
        logger.error(f"❌ Failed to create S3 client after {elapsed:.3f}s: {e}")
        return None

    try:
        # Define extra arguments for S3 upload based on content type
        extra_args = {
            'ContentType': content_type,
            'CacheControl': 'max-age=31536000'  # Cache for 1 year as per requirements
        }
        
        # Upload the file with optimized transfer configuration
        actual_upload_start = time.time()
        logger.info(f"🚀 Starting upload to S3: {object_name}")
        
        if file_size > 64 * MB:
            logger.info(f"📤 Large file detected ({file_size_mb:.2f}MB), using multipart upload")
        
        s3_client.upload_file(
            file_path, 
            S3_BUCKET_NAME, 
            object_name,
            ExtraArgs=extra_args,
            Config=transfer_config
        )
        
        actual_upload_elapsed = time.time() - actual_upload_start
        upload_speed = file_size_mb / actual_upload_elapsed if actual_upload_elapsed > 0 else 0
        logger.info(f"📤 Upload completed in {actual_upload_elapsed:.3f}s ({upload_speed:.2f} MB/s)")

        # Generate the URL
        url = f"https://{S3_BUCKET_NAME}.s3.{S3_REGION}.amazonaws.com/{object_name}"
        
        total_elapsed = time.time() - upload_start_time
        logger.info(f"🎉 S3 upload process completed in {total_elapsed:.3f}s total")
        logger.info(f"🔗 S3 URL: {url}")
        return url

    except ClientError as e:
        elapsed = time.time() - upload_start_time
        logger.error(f"❌ S3 ClientError after {elapsed:.3f}s: {e}")
        return None
    except Exception as e:
        elapsed = time.time() - upload_start_time
        logger.error(f"❌ S3 upload failed after {elapsed:.3f}s: {e}")
        return None

def upload_to_s3_async(file_path, object_name=None, content_type=None, callback=None):
    """
    Upload a file to S3 asynchronously in a background thread.
    
    :param file_path: File to upload
    :param object_name: S3 object name
    :param content_type: Content type
    :param callback: Function to call with result (url or None)
    """
    def async_upload():
        try:
            result_url = upload_to_s3(file_path, object_name, content_type)
            if callback:
                callback(result_url)
        except Exception as e:
            logger.error(f"Error in async S3 upload: {e}")
            if callback:
                callback(None)
    
    thread = threading.Thread(target=async_upload)
    thread.daemon = True
    thread.start()
    logger.info(f"Started async S3 upload thread for: {file_path}")

def get_content_type(file_path):
    """Determine the content type of a file based on its extension.
    
    :param file_path: Path to the file
    :return: Content type string
    """
    # Special cases for video formats
    extension = os.path.splitext(file_path)[1].lower()
    
    # Special mappings for video formats as per requirements
    video_types = {
        '.mp4': 'video/mp4',
        '.m3u8': 'application/x-mpegURL',  # HLS manifest
        '.m3u': 'application/x-mpegURL',   # HLS manifest
        '.ts': 'video/MP2T'                # HLS segments
    }
    
    if extension in video_types:
        return video_types[extension]
    
    # Use standard library for other types
    content_type, _ = mimetypes.guess_type(file_path)
    
    # Default to binary if type cannot be determined
    if content_type is None:
        return 'application/octet-stream'
        
    return content_type