"""
Pretend a job just ran, so you can test the round screen without AWS or Supabase.

Usage (run in a SECOND Terminal window while play_video.py is running):
    python3 fake_job.py success
    python3 fake_job.py failed

It does this:
    1. says "dispensing"  -> the screen shows the animation
    2. waits a few seconds
    3. says "success" or "failed" -> the screen shows the picture
    4. waits, then deletes fake_state.txt so play_video.py goes back to the real cloud
"""
import os
import sys
import time

FAKE_FILE = "fake_state.txt"
BREW_SECONDS = 8      # how long the animation plays before the result
SHOW_SECONDS = 8      # how long to wait after the result before cleaning up

result = sys.argv[1] if len(sys.argv) > 1 else "success"
if result not in ("success", "failed"):
    sys.exit("Use: python3 fake_job.py success   or   python3 fake_job.py failed")

try:
    print("Job started -> animation")
    with open(FAKE_FILE, "w") as f:
        f.write("dispensing")
    time.sleep(BREW_SECONDS)

    print(f"Job finished -> {result}")
    with open(FAKE_FILE, "w") as f:
        f.write(result)
    time.sleep(SHOW_SECONDS)
finally:
    if os.path.exists(FAKE_FILE):
        os.remove(FAKE_FILE)
    print("Done. Back to the real cloud state.")
