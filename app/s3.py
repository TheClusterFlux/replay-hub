import os
import boto3
from botocore.exceptions import ClientError
from app.config import AWS_ACCESS_KEY, AWS_SECRET_KEY, S3_BUCKET_NAME, S3_REGION


def upload_to_s3(file_path, object_name=None):
    """Upload a file to an S3 bucket and return the URL

    :param file_path: File to upload
    :param object_name: S3 object name (if not specified, file_name is used)
    :return: Public URL of the uploaded file or None if error
    """
    print(f"Starting upload to S3. File path: {file_path}, Object name: {object_name}")

    # If S3 object_name was not specified, use the filename from file_path
    if object_name is None:
        object_name = os.path.basename(file_path)
        print(f"No object name provided. Using file name as object name: {object_name}")

    # Create a boto3 client
    print("Creating S3 client...")
    s3_client = boto3.client(
        's3',
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY
    )
    print("S3 client created successfully.")

    try:
        # Upload the file
        print(f"Uploading file to S3 bucket: {S3_BUCKET_NAME}, Object name: {object_name}")
        s3_client.upload_file(
            file_path, 
            S3_BUCKET_NAME, 
            object_name
        )
        print(f"File uploaded successfully to bucket: {S3_BUCKET_NAME}, Object name: {object_name}")
        
        # Generate the URL
        url = f"https://{S3_BUCKET_NAME}.s3.{S3_REGION}.amazonaws.com/{object_name}"
        print(f"Generated S3 URL: {url}")
        return url

    except ClientError as e:
        print(f"Error uploading to S3: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error during S3 upload: {e}")
        return None