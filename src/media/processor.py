import os
import logging

logger = logging.getLogger("TelegramBackup")


def get_exif_date(filepath: str) -> str:
    """Extract 'Date Taken' from EXIF — gracefully falls back to mtime."""
    from datetime import datetime
    try:
        import piexif
        exif_dict = piexif.load(filepath)
        if "Exif" in exif_dict and piexif.ExifIFD.DateTimeOriginal in exif_dict["Exif"]:
            date_str = exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal].decode("utf-8")
            dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "Unknown"


def detect_faces(filepath: str) -> bool:
    """Detect faces using OpenCV — safe fallback if cv2 is missing."""
    try:
        import cv2
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        img = cv2.imread(filepath)
        if img is None:
            return False
        gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        return len(faces) > 0
    except Exception as e:
        logger.debug(f"Face detection unavailable ({filepath}): {e}")
        return False


async def compress_video(input_path: str, output_path: str,
                         target_size_mb: int = 500) -> str:
    """Compress large videos with FFmpeg. Returns original path if not needed/available."""
    import asyncio
    try:
        size_mb = os.path.getsize(input_path) / (1024 * 1024)
        if size_mb <= target_size_mb:
            return input_path

        logger.info(f"Compressing video {os.path.basename(input_path)} ({size_mb:.1f} MB)…")

        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vcodec", "libx264", "-crf", "28", "-preset", "fast",
            "-acodec", "aac", output_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode == 0 and os.path.exists(output_path):
            new_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"Compressed to {new_mb:.1f} MB")
            return output_path
        else:
            logger.warning(f"FFmpeg error: {stderr.decode()[:200]}")
            return input_path
    except Exception as e:
        logger.error(f"compress_video error: {e}")
        return input_path
