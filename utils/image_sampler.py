import cv2
import os
import numpy as np
from datetime import datetime
from multiprocessing import Process, Queue
from multiprocessing.queues import Empty
from utils.log_manager import LogManager
from io import BytesIO
from PIL import Image

try:
    import piexif
except ImportError as e:
    from utils.error_manager import DependencyError
    raise DependencyError('piexif', str(e))

logger = LogManager.get_logger(__name__)

def add_gps_exif(pil_image, gps_data):
    """
    Convert a PIL Image to JPEG bytes embedding GPS EXIF data if available."""
    buf = BytesIO()
    if not gps_data or 'latitude' not in gps_data or 'longitude' not in gps_data:
        pil_image.save(buf, format='JPEG')
        return buf.getvalue()
    try:
        lat = float(gps_data.get('latitude'))
        lon = float(gps_data.get('longitude'))
        lat_deg = int(lat)
        lat_min = int((lat - lat_deg) * 60)
        lat_sec = int((((lat - lat_deg) * 60) - lat_min) * 60 * 100)
        lon_deg = int(lon)
        lon_min = int((lon - lon_deg) * 60)
        lon_sec = int((((lon - lon_deg) * 60) - lon_min) * 60 * 100)

        exif_dict = {
            "GPS": {
                piexif.GPSIFD.GPSLatitudeRef: 'N' if lat >= 0 else 'S',
                piexif.GPSIFD.GPSLatitude: [(lat_deg, 1), (lat_min, 1), (lat_sec, 100)],
                piexif.GPSIFD.GPSLongitudeRef: 'E' if lon >= 0 else 'W',
                piexif.GPSIFD.GPSLongitude: [(lon_deg, 1), (lon_min, 1), (lon_sec, 100)]
            }
        }
        exif_bytes = piexif.dump(exif_dict)
        pil_image.save(buf, format='JPEG', exif=exif_bytes)
    except Exception as e:
        logger.error(f"Failed to embed GPS EXIF data: {e}")
        pil_image.save(buf, format='JPEG')
    return buf.getvalue()


class ImageRecorder:
    def __init__(self, save_directory, mode='whole', max_queue=200, new_process_threshold=90, max_processes=4):
        self.save_directory = save_directory
        self.mode = mode
        self.queue = Queue(maxsize=max_queue)
        self.new_process_threshold = new_process_threshold
        self.max_processes = max_processes
        self.processes = []
        self.running = True
        self.logger = LogManager.get_logger(__name__)

        self.start_new_process()

    def start_new_process(self):
        if len(self.processes) < self.max_processes:
            p = Process(target=self.save_images)
            p.start()
            self.processes.append(p)
            self.logger.info(f"[INFO] Started new process, total processes: {len(self.processes)}")
        else:
            self.logger.warning("[INFO] Maximum number of processes reached.")

    def save_images(self):
        while self.running or not self.queue.empty():
            try:
                frame, frame_id, boxes, centres, gps_data = self.queue.get(timeout=3)
            except Empty:
                if not self.running:
                    break
                continue
            except KeyboardInterrupt:
                self.logger.info("[INFO] KeyboardInterrupt received in save_images. Exiting.")
                break

            self.process_frame(frame, frame_id, boxes, centres, gps_data)

    def process_frame(self, frame, frame_id, boxes, centres, gps_data):
        timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H%M%S.%f')[:-3] + 'Z'
        if self.mode == 'whole':
            self.save_frame(frame, frame_id, timestamp, gps_data)
        elif self.mode == 'bbox':
            self.save_bboxes(frame, frame_id, boxes, timestamp, gps_data)
        elif self.mode == 'square':
            self.save_squares(frame, frame_id, centres, timestamp, gps_data)

    def save_frame(self, frame, frame_id, timestamp, gps_data):
        filename = f"{timestamp}_frame_{frame_id}.jpg"
        filepath = os.path.join(self.save_directory, filename)
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        image_bytes = add_gps_exif(image, gps_data)
        with open(filepath, 'wb') as f:
            f.write(image_bytes)

    def save_bboxes(self, frame, frame_id, boxes, timestamp, gps_data):
        for contour_id, box in enumerate(boxes):
            startX, startY, width, height = box
            cropped_image = frame[startY:startY+height, startX:startX+width]
            filename = f"{timestamp}_frame_{frame_id}_n_{str(contour_id)}.jpg"
            filepath = os.path.join(self.save_directory, filename)
            image = Image.fromarray(cv2.cvtColor(cropped_image, cv2.COLOR_BGR2RGB))
            image_bytes = add_gps_exif(image, gps_data)
            with open(filepath, 'wb') as f:
                f.write(image_bytes)

    def save_squares(self, frame, frame_id, centres, timestamp, gps_data):
        side_length = min(200, frame.shape[0])
        halfLength = side_length // 2
        for contour_id, centre in enumerate(centres):
            startX = max(centre[0] - np.random.randint(10, halfLength), 0)
            startY = max(centre[1] - np.random.randint(10, halfLength), 0)
            endX = startX + side_length
            endY = startY + side_length
            if endX > frame.shape[1]:
                startX = frame.shape[1] - side_length
            if endY > frame.shape[0]:
                startY = frame.shape[0] - side_length
            square_image = frame[startY:endY, startX:endX]
            filename = f"{timestamp}_frame_{frame_id}_n_{str(contour_id)}.jpg"
            filepath = os.path.join(self.save_directory, filename)
            image = Image.fromarray(cv2.cvtColor(square_image, cv2.COLOR_BGR2RGB))
            image_bytes = add_gps_exif(image, gps_data)
            with open(filepath, 'wb') as f:
                f.write(image_bytes)

    def add_frame(self, frame, frame_id, boxes, centres, gps_data=None):
        if not self.queue.full():
            self.queue.put((frame, frame_id, boxes, centres, gps_data))
        else:
            self.logger.info("[INFO] Queue is full, spinning up new process. Frame skipped.")

        if self.queue.qsize() > self.new_process_threshold and len(self.processes) < self.max_processes:
            self.start_new_process()

    def stop(self):
        """Stop image recording processes and clean up resources."""
        self.running = False

        try:
            while not self.queue.empty():
                self.queue.get_nowait()
        except Exception as e:
            self.logger.warning(f"Failed to clear queue: {e}")

        self.queue.close()
        self.queue.join_thread()

        for p in self.processes:
            try:
                p.join(timeout=1)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=0.5)
            except Exception as e:
                self.logger.error(f"Failed to stop process: {e}")

        self.processes.clear()
        self.logger.info("[INFO] ImageRecorder stopped.")

    def terminate(self):
        """Force terminate all image recording processes."""
        self.running = False
        for p in self.processes:
            if p.is_alive():
                try:
                    p.terminate()
                    p.join(timeout=0.5)
                except Exception as e:
                    self.logger.error(f"Failed to terminate process: {e}")

        self.processes.clear()
        self.queue.close()
        self.queue.join_thread()
        self.logger.info("[INFO] All recording processes terminated forcefully.")
