import os
import logging
import piexif
import cv2
import ffmpeg
import subprocess
from datetime import datetime

logger = logging.getLogger("TelegramBackup")

def get_exif_date(filepath: str) -> str:
    """Extract 'Date Taken' from EXIF data."""
    try:
        exif_dict = piexif.load(filepath)
        if "Exif" in exif_dict and piexif.ExifIFD.DateTimeOriginal in exif_dict["Exif"]:
            date_str = exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal].decode("utf-8")
            # Format: YYYY:MM:DD HH:MM:SS
            dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    
    # Fallback to mtime if EXIF fails
    try:
        return datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "Unknown"

def detect_faces(filepath: str) -> bool:
    """Detect if a photo has faces using OpenCV."""
    try:
        # Load pre-trained face detector (Haar Cascade)
        # On Mac/Windows, we might need to point to the actual path or bundle it.
        # For simplicity, we try to find it in common locations or use a best-effort approach.
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        
        img = cv2.imread(filepath)
        if img is None:
            return False
            
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        
        return len(faces) > 0
    except Exception as e:
        logger.debug(f"Face detection failed for {filepath}: {e}")
        return False

async def compress_video(input_path: str, output_path: str, target_size_mb: int = 500) -> str:
    """Compress video using FFmpeg if it exceeds the limit."""
    try:
        # Check if ffmpeg is installed
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        
        size_mb = os.path.getsize(input_path) / (1024 * 1024)
        if size_mb <= target_size_mb:
            return input_path
            
        logger.info(f"Compressing {os.path.basename(input_path)} ({size_mb:.1f}MB > {target_size_mb}MB)")
        
        # Simple compression logic: use crf 28 for a good balance
        (
            ffmpeg
            .input(input_path)
            .output(output_path, vcodec='libx264', crf=28, preset='fast', acodec='aac')
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        
        new_size = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(f"Compressed to {new_size:.1f}MB")
        return output_path
    except Exception as e:
        logger.error(f"Compression failed: {e}")
        return input_path
