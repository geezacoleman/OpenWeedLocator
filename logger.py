import sqlite3
import threading
import queue
import os
from datetime import datetime, timezone
from time import strftime

class AsyncDBLogger:
    def __init__(self, db_path):
        self.db_path = db_path
        self.log_queue = queue.Queue()
        self.running = True
        self.thread = threading.Thread(target=self._process_log_queue)
        self.thread.start()
        self._setup_db()

    def _setup_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY,
                    timestamp TIMESTAMP,
                    message TEXT
                )
            """)
            conn.commit()

    def _process_log_queue(self):
        while self.running or not self.log_queue.empty():
            try:
                log_entry = self.log_queue.get(timeout=1)
                self._insert_log(log_entry)
                self.log_queue.task_done()
            except queue.Empty:
                continue

    def _insert_log(self, log_entry):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO logs (timestamp, message)
                VALUES (?, ?)
            """, log_entry)
            conn.commit()

    def log(self, timestamp, message):
        self.log_queue.put((timestamp, message))

    def stop(self):
        self.running = False
        self.thread.join()

class Logger:
    def __init__(self, db_name, saveDir):
        self.db_name = strftime("%Y%m%d-%H%M%S_") + db_name
        self.saveDir = saveDir
        if not os.path.exists(self.saveDir):
            os.makedirs(self.saveDir)

        self.db_path = os.path.join(self.saveDir, self.db_name)
        self.dbLogger = AsyncDBLogger(self.db_path)

    def log_line(self, line, verbose=False):
        timestamp = datetime.now(timezone.utc)
        formatted_line = f"{timestamp} {line}\n"
        if verbose:
            print(formatted_line)
        self.dbLogger.log(timestamp, line)

    def log_line_video(self, line, verbose):
        self.log_line(line, verbose=False)
        self.videoLine = f"{datetime.now(timezone.utc)} {line}\n"
        if verbose:
            print(line)
        # Add code here if you need to handle video logs differently

    def new_video_logfile(self, name):
        self.videoLog = name
        self.log_line_video(f'NEW VIDEO LOG CREATED {name}', verbose=True)

    def stop_logging(self):
        self.dbLogger.stop()