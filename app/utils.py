import os
import time
import uuid
from moviepy.editor import VideoFileClip
from app import logger

def extract_video_metadata(file_path):
    """Extract metadata from a video file."""
    try:
        logger.info(f"Extracting metadata from video: {file_path}")
        clip = VideoFileClip(file_path)

        metadata = {
            "duration": clip.duration,
            "resolution": f"{clip.size[0]}x{clip.size[1]}",
            "fps": clip.fps,
            "file_path": file_path  # Add the file path to the metadata
        }

        # Generate a thumbnail
        thumbnail_path = file_path + "_thumbnail.jpg"
        clip.save_frame(thumbnail_path, t=0)
        metadata["thumbnail_path"] = thumbnail_path

        logger.info(f"Extracted metadata: {metadata}")
        return metadata
    except Exception as e:
        logger.error(f"Error extracting metadata: {e}")
        raise
    finally:
        # Just close the clip but don't delete the file here
        # The file will be deleted after the entire upload process is complete
        if 'clip' in locals() and clip:
            clip.close()

def schedule_delete(file_path, delay):
    """Schedule a file for deletion after a delay."""
    try:
        logger.info(f"Scheduling deletion of file: {file_path} in {delay} seconds")
        time.sleep(delay)
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"File deleted: {file_path}")
        else:
            logger.warning(f"File not found for deletion: {file_path}")
    except Exception as e:
        logger.error(f"Error deleting file: {e}")