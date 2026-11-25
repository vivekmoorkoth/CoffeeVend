"""
CoffeeVend display controller for the Arduino Uno + round GC9A01 screen.

It plays a different animation depending on what the cloud says is happening:
    idle        -> loops idle.mp4
    dispensing  -> loops brewing.mp4
    success     -> plays success.mp4 once, then goes back to idle
    failed      -> plays failed.mp4 once, then goes back to idle

The state comes from GET /display-state on your API (see the Lambda change).

Usage:
    python3 play_video.py /dev/cu.usbmodem101
(The four .mp4 files must be in the same folder as this script.)
"""
import os
import sys
import threading
import time

import cv2
import numpy as np
import requests
import serial

# ---------- screen / serial settings (unchanged from before) ----------
SIZE = 80
CHUNK = 4
BAUD = 1000000
FIT = 0.85

# ---------- CoffeeVend settings (EDIT THESE TWO IF NEEDED) ----------
API = "https://9d805awoce.execute-api.us-east-2.amazonaws.com"
MACHINE_ID = "coffeevend_01"

# Which video file goes with which state
CLIPS = {
    "idle": "idle screen.png",                  # the how-to-use picture
    "dispensing": "blerg0001-0208 (1).mp4",     # the animation while a job runs
    "success": "success.png",   # a still picture works too (it is held on screen)
    "failed": "failed.png",
}
ONCE_CLIPS = ("success", "failed")   # these play one time, then return to idle
# A result stays on the screen at least this many seconds before going back to idle.
# This is what makes a single still picture (a PNG) stay visible.
MIN_SECONDS = {"success": 5, "failed": 5}
FAKE_FILE = "fake_state.txt"   # testing only: if this file exists, it replaces the cloud
POLL_SECONDS = 1.5                   # how often we ask the cloud what's happening

# The latest answer from the cloud: (state, job_id). Stored as one tuple so the
# polling thread and the video loop never see half of an update.
current = {"value": ("idle", None)}

# Testing helpers: when the fake file is deleted, the screen goes back to the real
# cloud. Whatever finished job the cloud is still reporting at that moment is OLD,
# so we remember it and don't play its result again.
fake_used = {"on": False}
ignore_job = {"id": None}


def convert(frame):
    h, w = frame.shape[:2]
    s = min(h, w)
    frame = frame[(h - s) // 2:(h - s) // 2 + s, (w - s) // 2:(w - s) // 2 + s]
    inner = int(SIZE * FIT)
    small = cv2.resize(frame, (inner, inner), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((SIZE, SIZE, 3), dtype=np.uint8)
    off = (SIZE - inner) // 2
    canvas[off:off + inner, off:off + inner] = small
    # OpenCV reads pictures and videos as Blue-Green-Red. Flip to Red-Green-Blue
    # so the screen shows the true colors (without this, reds look blue).
    rgb = canvas[:, :, ::-1]
    r, g, b = rgb[:, :, 0] >> 5, rgb[:, :, 1] >> 5, rgb[:, :, 2] >> 6
    return ((r << 5) | (g << 2) | b).astype(np.uint8)


def load_clip(path):
    """Read one video (or one still picture) and return its converted frames."""
    if path.lower().endswith((".png", ".jpg", ".jpeg")):
        img = cv2.imread(path)
        if img is None:
            sys.exit(f"Could not read the picture '{path}'. "
                     f"Is it in the same folder as this script?")
        return [convert(img)]          # a picture is just one frame
    cap = cv2.VideoCapture(path)
    frames = []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        frames.append(convert(img))
    cap.release()
    if not frames:
        sys.exit(f"Could not read any frames from '{path}'. "
                 f"Is it in the same folder as this script?")
    return frames[::6]


def poll_once():
    """Ask the cloud for the display state. Returns True if it worked."""
    if os.path.exists(FAKE_FILE):          # testing: pretend the cloud said this
        with open(FAKE_FILE) as f:
            word = f.read().strip() or "idle"
        current["value"] = (word, os.path.getmtime(FAKE_FILE))
        fake_used["on"] = True
        return True
    try:
        r = requests.get(f"{API}/display-state",
                         params={"machine_id": MACHINE_ID}, timeout=5)
        data = r.json()
        new = (data["state"], data["job_id"])
        if fake_used["on"]:                 # we just came back from fake mode
            fake_used["on"] = False
            if new[0] in ONCE_CLIPS:
                ignore_job["id"] = new[1]
        if new[0] in ONCE_CLIPS and new[1] == ignore_job["id"]:
            new = ("idle", new[1])          # an old result: treat as nothing new
        if new != current["value"]:
            print(f"Cloud says: {new[0]} (job {new[1]})")
        current["value"] = new
        return True
    except (requests.RequestException, ValueError, KeyError):
        return False   # network blip or bad reply: keep the last known state


def poll_forever():
    while True:
        poll_once()
        time.sleep(POLL_SECONDS)


def advance(clip, idx, state, job_id, shown_job, lengths, elapsed=999):
    """
    Decide which clip and frame come next.
    Returns (clip, idx, shown_job).
    shown_job = the last finished job whose success/fail clip we already played,
    so the same result is never played twice.
    """
    # "finished" = last frame reached AND it has been on screen long enough
    finished = idx >= lengths[clip] - 1 and elapsed >= MIN_SECONDS.get(clip, 0)

    # A new order started: show the brewing animation.
    if state == "dispensing" and clip != "dispensing":
        return "dispensing", 0, shown_job
    # A job just finished and we haven't shown its result yet: play it once.
    if state in ("success", "failed") and job_id != shown_job:
        return state, 0, job_id
    # A one-time clip reached its last frame: go back to idle.
    if clip in ONCE_CLIPS and finished:
        return "idle", 0, shown_job
    # Brewing is over (job gone or finished): go back to idle.
    if clip == "dispensing" and state != "dispensing":
        return "idle", 0, shown_job
    # Otherwise just keep going with the same clip, looping at the end.
    return clip, (idx + 1) % lengths[clip], shown_job


def main():
    port = sys.argv[1]

    # Load all four videos up front (takes a little while).
    frames, loaded = {}, {}
    for name, path in CLIPS.items():
        if path not in loaded:             # don't re-read a file used by two states
            loaded[path] = load_clip(path)
        frames[name] = loaded[path]
        print(f"Loaded '{name}': {len(frames[name])} frames")
    lengths = {name: len(f) for name, f in frames.items()}

    # Ask the cloud once before starting, so an old finished job isn't replayed.
    for _ in range(5):
        if poll_once():
            break
        print("Can't reach the API yet, retrying...")
        time.sleep(2)
    state, job_id = current["value"]
    shown_job = job_id if state in ONCE_CLIPS else None

    threading.Thread(target=poll_forever, daemon=True).start()

    ser = serial.Serial(port, BAUD, timeout=2)
    time.sleep(2)
    ser.reset_input_buffer()
    print("Playing... press Ctrl+C to stop.")

    clip, idx = "idle", -1
    clip_started = time.time()
    frame, y = None, 0
    try:
        while True:
            cmd = ser.read(1)
            if cmd == b"F":
                state, job_id = current["value"]
                new_clip, idx, shown_job = advance(
                    clip, idx, state, job_id, shown_job, lengths,
                    time.time() - clip_started)
                if new_clip != clip:
                    print(f"Now showing: {new_clip}")
                    clip = new_clip
                    clip_started = time.time()
                frame, y = frames[clip][idx], 0
            elif cmd == b"R" and frame is not None and y < SIZE:
                ser.write(frame[y:y + CHUNK].tobytes())
                y += CHUNK
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
