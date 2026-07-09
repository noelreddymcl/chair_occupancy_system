"""
calibrate_chairs.py
One-time setup tool: grabs a frame from your camera, lets you draw a box
around each chair with the mouse, and saves those boxes permanently as
"chair zones" (data/chair_zones.json + the chairs table in SQLite).

Why: chairs don't move, but YOLO's "chair" class is inconsistent frame to
frame -- especially once a person sits down and blocks most of the chair
from view. Drawing the zones once removes that guesswork: the live
detector only has to detect PEOPLE and check them against these fixed
regions, which is a much easier and more reliable problem.

Run:
  python calibrate_chairs.py                # webcam index 0
  VIDEO_SOURCE=demo.mp4 python calibrate_chairs.py

Controls:
  - Click and drag to draw a box around a chair
  - Release to confirm that box (it gets a label like "Chair 1")
  - Press 'u' to undo the last box
  - Press 'r' to clear everything and start over
  - Press 's' to save all boxes and exit
  - Press 'q' / Esc to quit without saving
"""

import os
import sys
import json

import cv2

import database as db

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ZONES_PATH = os.path.join(DATA_DIR, "chair_zones.json")


class BoxDrawer:
    def __init__(self, frame):
        self.frame = frame
        self.boxes = []  # list of (x1, y1, x2, y2)
        self.drawing = False
        self.start = None
        self.current = None

    def on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.drawing = True
            self.start = (x, y)
            self.current = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and self.drawing:
            self.current = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and self.drawing:
            self.drawing = False
            x1, y1 = self.start
            x2, y2 = self.current
            box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            if box[2] - box[0] > 15 and box[3] - box[1] > 15:  # ignore accidental tiny clicks
                self.boxes.append(box)
            self.start = None
            self.current = None

    def render(self):
        img = self.frame.copy()
        for i, (x1, y1, x2, y2) in enumerate(self.boxes, start=1):
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
            cv2.putText(img, f"Chair {i}", (x1, max(20, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2, cv2.LINE_AA)
        if self.drawing and self.start and self.current:
            cv2.rectangle(img, self.start, self.current, (0, 165, 255), 2)

        cv2.putText(img, "drag=draw box  u=undo  r=reset  s=save  q=quit",
                    (10, img.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
        return img


def grab_reference_frame(source):
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"Could not open video source: {source}")
        sys.exit(1)

    print("Camera opened. A preview window will appear -- press SPACE to")
    print("freeze the frame you want to calibrate on, once the room looks right.")

    frame = None
    while True:
        ok, live = cap.read()
        if not ok:
            continue
        preview = live.copy()
        cv2.putText(preview, "press SPACE to freeze this frame", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow("calibrate - live preview", preview)
        key = cv2.waitKey(1) & 0xFF
        if key == 32:  # space
            frame = live.copy()
            break
        if key in (27, ord("q")):
            cap.release()
            cv2.destroyAllWindows()
            sys.exit(0)

    cap.release()
    cv2.destroyWindow("calibrate - live preview")
    return frame


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VIDEO_SOURCE", "0")
    source = int(source) if str(source).isdigit() else source

    frame = grab_reference_frame(source)

    drawer = BoxDrawer(frame)
    win = "calibrate - draw chair boxes"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, drawer.on_mouse) 

    while True:
        cv2.imshow(win, drawer.render())
        key = cv2.waitKey(20) & 0xFF

        if key == ord("u") and drawer.boxes:
            drawer.boxes.pop()
        elif key == ord("r"):
            drawer.boxes = []
        elif key == ord("s"):
            break
        elif key in (27, ord("q")):
            cv2.destroyAllWindows()
            sys.exit(0)

    cv2.destroyAllWindows()

    if not drawer.boxes:
        print("No boxes drawn -- nothing saved.")
        return

    db.init_db()
    os.makedirs(DATA_DIR, exist_ok=True)
    zones = []
    for i, box in enumerate(drawer.boxes, start=1):
        label = f"Chair {i}"
        chair_id = db.get_or_create_chair(label)
        zones.append({"chair_id": chair_id, "label": label, "box": list(box)})

    with open(ZONES_PATH, "w") as f:
        json.dump(zones, f, indent=2)

    print(f"Saved {len(zones)} chair zone(s) to {ZONES_PATH}")
    for z in zones:
        print(f"  - {z['label']}: {z['box']}")
    print("\nYou can now run: python app.py")


if __name__ == "__main__":
    main()
