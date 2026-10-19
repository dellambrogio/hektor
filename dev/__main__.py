"""Hektor's developer tools (dev/run.sh TOOL).

  console          try the noises, the mic level and the voice in a window
  monitor [HOST]   watch Hektor on hektor (HOST: user@machine, default HEKTOR_HOST from deploy.conf;
                   "local" for this computer), its CPU and RAM, and the worker on monitor
  mic-test         the mic level live against the speech threshold, in the terminal
  wake-test        what the wake word spotter hears, in the terminal
  sound NAME       play one of Hektor's noises ("all" for each in turn)
  screenshots      redraw the pictures in doc/ (the eyes, the monitor app with demo data)
  faces [forget NAME | rename OLD NEW]  who Hektor knows by face (run it on hektor); forget, rename
"""
import argparse
import logging
import os

os.environ.setdefault("SDL_VIDEODRIVER", "wayland,x11")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from hektor import config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(prog="dev/run.sh", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool", choices=["console", "monitor", "mic-test", "wake-test", "sound", "faces", "screenshots"])
    parser.add_argument("arg", nargs="*", help="monitor: the host; sound: the noise; faces: forget NAME, rename OLD NEW")
    parser.add_argument("--debug", action="store_true", help="log more")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if args.tool == "monitor":
        from .monitor import run
        run(args.arg[0] if args.arg else None)
        return
    if args.tool == "screenshots":
        from .screenshots import run
        run()
        return
    if args.tool == "faces":
        from .faces import run
        run(args.arg)
        return
    conf = config.load()
    match args.tool:
        case "console":
            from .console import run
            run(conf)
        case "mic-test":
            from .mic import mic_test
            mic_test(conf)
        case "wake-test":
            from .mic import wake_test
            wake_test(conf)
        case "sound":
            from .sounds import play_once
            if not args.arg:
                parser.error("sound needs a NAME (or all)")
            play_once(conf, args.arg[0])


main()
