import os
import boto3
import mimetypes
from botocore.exceptions import ClientError
from app.config import AWS_ACCESS_KEY, AWS_SECRET_KEY, S3_BUCKET_NAME, S3_REGION
import logging

# Configure logger
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def upload_to_s3(file_path, object_name=None, content_type=None):
    """Upload a file to an S3 bucket and return the URL

    :param file_path: File to upload
    :param object_name: S3 object name (if not specified, file_name is used)
    :param content_type: Optional content type override
    :return: Public URL of the uploaded file or None if error
    """
    logger.info(f"Starting upload to S3. File path: {file_path}, Object name: {object_name}")

    # If S3 object_name was not specified, use the filename from file_path
    if object_name is None:
        object_name = os.path.basename(file_path)
        logger.info(f"No object name provided. Using file name as object name: {object_name}")

    # Verify that the file exists
    if not os.path.exists(file_path):
        logger.error(f"File does not exist: {file_path}")
        return None

    # Determine the file's content type if not provided
    if content_type is None:
        content_type = get_content_type(file_path)
        logger.info(f"Detected content type: {content_type}")

    # Log AWS configuration details (without exposing sensitive information)
    logger.info(f"AWS Region: {S3_REGION}, Bucket Name: {S3_BUCKET_NAME}")

    # Log AWS credentials and region for debugging (do not log secrets in production)
    logger.info(f"AWS_ACCESS_KEY: {AWS_ACCESS_KEY[:4]}... (truncated for security)")
    logger.info(f"AWS_SECRET_KEY: {AWS_SECRET_KEY[:4]}... (truncated for security)")
    logger.info(f"S3_REGION: {S3_REGION}")

    # Create a boto3 client
    try:
        logger.info("Creating S3 client...")
        s3_client = boto3.client(
            's3',
            region_name=S3_REGION,
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY
        )
        logger.info("S3 client created successfully.")
    except Exception as e:
        logger.error(f"Failed to create S3 client: {e}")
        return None

    try:
        # Define extra arguments for S3 upload based on content type
        extra_args = {
            'ContentType': content_type,
            'CacheControl': 'max-age=31536000'  # Cache for 1 year as per requirements
        }
        
        # Upload the file
        logger.info(f"Uploading file to S3 bucket: {S3_BUCKET_NAME}, Object name: {object_name}")
        s3_client.upload_file(
            file_path, 
            S3_BUCKET_NAME, 
            object_name,
            ExtraArgs=extra_args
        )
        logger.info(f"File uploaded successfully to bucket: {S3_BUCKET_NAME}, Object name: {object_name}")

        # Generate the URL
        url = f"https://{S3_BUCKET_NAME}.s3.{S3_REGION}.amazonaws.com/{object_name}"
        logger.info(f"Generated S3 URL: {url}")
        return url

    except ClientError as e:
        logger.error(f"ClientError during S3 upload: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error during S3 upload: {e}")
        return None


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