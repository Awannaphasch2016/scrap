"""Background screen recorder for an opencli session.

Polls `opencli browser <sess> screenshot` at a configurable rate, saves frames
to an output directory as `frame_NNNNN.png`. Designed to be spawned as a
subprocess by the wrapper:

  recorder = subprocess.Popen([
      sys.executable, "scripts/screen_recorder.py",
      "--session", "cursor",
      "--output-dir", "/tmp/cursor_recording_<ts>",
      "--fps", "1",
  ])
  try:
      main()
  finally:
      recorder.terminate()
      recorder.wait(timeout=5)

After the run, stitch frames into MP4:
  ffmpeg -framerate <fps> -i /tmp/X/frame_%05d.png -c:v libx264 \
    -pix_fmt yuv420p /tmp/X.mp4
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="opencli session screen recorder")
    ap.add_argument("--session", required=True,
                    help="opencli session name to record")
    ap.add_argument("--output-dir", required=True,
                    help="Directory to write frame PNGs into")
    ap.add_argument("--fps", type=float, default=1.0,
                    help="Frames per second (default 1.0)")
    ap.add_argument("--max-frames", type=int, default=600,
                    help="Stop after this many frames (default 600 ≈ 10min @ 1fps)")
    args = ap.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    interval = 1.0 / max(0.1, args.fps)

    state = {"frame": 0, "stop": False}

    def _stop(*_):
        state["stop"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"[recorder] session={args.session} out={out} fps={args.fps} "
          f"max_frames={args.max_frames}", file=sys.stderr)

    while not state["stop"] and state["frame"] < args.max_frames:
        path = out / f"frame_{state['frame']:05d}.png"
        try:
            subprocess.run(
                ["opencli", "browser", args.session,
                 "--window", "background", "screenshot", str(path)],
                capture_output=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            print(f"[recorder] screenshot timeout at frame {state['frame']}",
                  file=sys.stderr)
        state["frame"] += 1
        time.sleep(interval)

    print(f"[recorder] stopped after {state['frame']} frames", file=sys.stderr)


if __name__ == "__main__":
    main()
