"""
detector.py
Core computer-vision engine for the Seat Occupancy System.

Pipeline:
  1. Pull frames from a camera / video file (OpenCV).
  2. Run YOLOv8 (COCO weights) to find "chair" and "person" boxes.
  3. Match detected chairs frame-to-frame by centroid distance so each
     physical chair keeps a stable ID (chairs don't move, people do).
  4. A chair is "occupied" when a person box overlaps it enough.
  5. State changes are debounced over N consecutive frames to avoid
     flicker (e.g. someone briefly leaning over a chair).
  6. Every confirmed empty -> occupied -> empty cycle is written to
     SQLite as one session with a duration, exactly like the
     "empty chair found 10:20, next empty 10:30 -> sat 10 min" example.

Designed to run on a laptop CPU today and drop onto a Raspberry Pi /
ARM board later (swap `yolov8n.pt` for a smaller / NCNN-exported model
if you need more FPS on-device).
"""

import time
import threading
from datetime import datetime

import cv2
import numpy as np
from ultralytics import YOLO

import database as db

COCO_PERSON = 0
COCO_CHAIR = 56

# how much of the chair box a person must cover to count as "sitting"
OVERLAP_THRESHOLD = 0.15
# consecutive frames a state must hold before it's confirmed (debounce)
CONFIRM_FRAMES = 5
# max pixel distance to match a chair detection to a known chair across frames
CHAIR_MATCH_DIST = 80


def iou_overlap_ratio(person_box, chair_box):
    """Fraction of the chair box area covered by the person box."""
    px1, py1, px2, py2 = person_box
    cx1, cy1, cx2, cy2 = chair_box

    ix1, iy1 = max(px1, cx1), max(py1, cy1)
    ix2, iy2 = min(px2, cx2), min(py2, cy2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    inter = (ix2 - ix1) * (iy2 - iy1)
    chair_area = max(1.0, (cx2 - cx1) * (cy2 - cy1))
    return inter / chair_area


def centroid(box):
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


class ChairTracker:
    """Keeps stable IDs for chairs seen across frames and their debounced
    occupancy state, and talks to the database when state is confirmed."""

    def __init__(self):
        self.known_chairs = {}
        self.pending = {}
        self.confirmed = {}
        self.next_label_idx = 1
        self.lock = threading.Lock()

    def _match_or_register(self, box):
        c = centroid(box)
        best_id, best_dist = None, CHAIR_MATCH_DIST
        for chair_id, info in self.known_chairs.items():
            kx, ky = info["centroid"]
            dist = ((kx - c[0]) ** 2 + (ky - c[1]) ** 2) ** 0.5
            if dist < best_dist:
                best_dist, best_id = dist, chair_id

        if best_id is not None:
            self.known_chairs[best_id]["centroid"] = c
            self.known_chairs[best_id]["box"] = box
            return best_id

        label = f"Chair {self.next_label_idx}"
        self.next_label_idx += 1
        chair_id = db.get_or_create_chair(label)
        self.known_chairs[chair_id] = {"centroid": c, "box": box, "label": label}
        self.confirmed[chair_id] = False
        self.pending[chair_id] = {"state": False, "count": 0}
        return chair_id

    def update(self, chair_boxes, person_boxes, when: datetime):
        with self.lock:
            # register/update positions for any chairs detected this frame
            for cbox in chair_boxes:
                self._match_or_register(cbox)

            # check EVERY known chair against people, using its last known
            # box — chairs are static furniture, so we keep evaluating them
            # even in frames where a sitting person blocks the chair itself
            # from being re-detected.
            for chair_id, info in self.known_chairs.items():
                cbox = info["box"]
                raw_occupied = any(
                    iou_overlap_ratio(pbox, cbox) >= OVERLAP_THRESHOLD for pbox in person_boxes
                )
                self._apply_debounced(chair_id, raw_occupied, when)

            return self.snapshot()

    def _apply_debounced(self, chair_id, raw_state, when):
        confirmed_state = self.confirmed.get(chair_id, False)
        pend = self.pending.setdefault(chair_id, {"state": raw_state, "count": 0})

        if raw_state == confirmed_state:
            pend["state"], pend["count"] = raw_state, 0
            db.touch_status(chair_id, confirmed_state, when)
            return

        if pend["state"] == raw_state:
            pend["count"] += 1
        else:
            pend["state"], pend["count"] = raw_state, 1

        if pend["count"] >= CONFIRM_FRAMES:
            self.confirmed[chair_id] = raw_state
            pend["count"] = 0
            if raw_state:
                db.start_session(chair_id, when)
            else:
                open_sess = db.get_open_session(chair_id)
                if open_sess:
                    start = datetime.fromisoformat(open_sess["start_time"])
                    duration = (when - start).total_seconds()
                    db.end_session(open_sess["id"], when, duration)

    def snapshot(self):
        out = []
        for chair_id, info in self.known_chairs.items():
            out.append({
                "chair_id": chair_id,
                "label": info.get("label", str(chair_id)),
                "box": info["box"],
                "occupied": self.confirmed.get(chair_id, False),
            })
        return out


class OccupancyEngine:
    """Owns the video capture + YOLO model and runs detection in a
    background thread. `get_jpeg` can be polled by a Flask route to
    serve a live annotated snapshot / MJPEG stream."""

    def __init__(self, source=0, model_path="yolov8n.pt", frame_skip=2):
        self.source = source
        self.model_path = model_path
        self.frame_skip = frame_skip
        self.tracker = ChairTracker()
        self.model = None
        self.running = False
        self.thread = None
        self.latest_jpeg = None
        self.latest_state = []
        self._frame_lock = threading.Lock()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self):
        self.model = YOLO(self.model_path)
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            print(f"[detector] could not open video source: {self.source}")
            self.running = False
            return

        frame_idx = 0
        while self.running:
            ok, frame = cap.read()
            if not ok:
                if isinstance(self.source, str):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                time.sleep(0.5)
                continue

            frame_idx += 1
            if frame_idx % self.frame_skip != 0:
                continue

            results = self.model(frame, classes=[COCO_PERSON, COCO_CHAIR], verbose=False)[0]

            chair_boxes, person_boxes = [], []
            for b in results.boxes:
                cls = int(b.cls[0])
                xyxy = tuple(float(v) for v in b.xyxy[0])
                if cls == COCO_CHAIR:
                    chair_boxes.append(xyxy)
                elif cls == COCO_PERSON:
                    person_boxes.append(xyxy)

            now = datetime.utcnow()
            state = self.tracker.update(chair_boxes, person_boxes, now)

            annotated = self._draw(frame, state, person_boxes)
            ok, buf = cv2.imencode(".jpg", annotated)
            if ok:
                with self._frame_lock:
                    self.latest_jpeg = buf.tobytes()
                    self.latest_state = state

        cap.release()

    def _draw(self, frame, chair_states, person_boxes):
        for pbox in person_boxes:
            x1, y1, x2, y2 = (int(v) for v in pbox)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 200, 0), 1)

        for c in chair_states:
            x1, y1, x2, y2 = (int(v) for v in c["box"])
            color = (0, 0, 255) if c["occupied"] else (0, 200, 0)
            label = f"{c['label']}: {'OCCUPIED' if c['occupied'] else 'empty'}"
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, label, (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, color, 2, cv2.LINE_AA)
        return frame

    def get_jpeg(self):
        with self._frame_lock:
            return self.latest_jpeg

    def get_state(self):
        with self._frame_lock:
            return list(self.latest_state)


if __name__ == "__main__":
    import sys
    db.init_db()
    src = sys.argv[1] if len(sys.argv) > 1 else 0
    src = int(src) if str(src).isdigit() else src
    engine = OccupancyEngine(source=src)
    engine.start()
    print("Running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
            print(engine.get_state())
    except KeyboardInterrupt:
        engine.stop()
