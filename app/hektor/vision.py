"""Camera: finds faces, so the eyes can look at you and Hektor knows when someone's there.

A thread reads the webcam and runs OpenCV's YuNet face detector a few times a second. The face reads
`face` (where the biggest face is, as a gaze target) and `seen` (when a face was last there). With
FACE_RECOGNITION it also tells who that is (`person`, faces.py): when the face shows up, then every few
seconds; and it collects the fingerprints when someone asks to be remembered (`enroll`).

While the monitor (dev/monitor.py) is watching, the small picture with the faces boxed is shared with
it (share.py).
"""
import logging
import threading
import time
from pathlib import Path

import numpy as np

from . import share
from .config import Conf

log = logging.getLogger(__name__)
DETECT_SIZE = (320, 240)
RECHECK = 3.0          # seconds between checks of who the face is, once known


def find_camera(preferred: list[str]) -> int | None:
    """Index of the webcam to use: a `preferred` one (name contains it), else the first color camera.

    Laptops also have an infrared camera for face login, which only gives grey frames, and every
    camera has a second "metadata" device that gives none; both are skipped.
    """
    import cv2
    cams = []
    for d in sorted(Path("/sys/class/video4linux").glob("video*"), key=lambda p: int(p.name[5:])):
        try:
            if (d / "index").read_text().strip() != "0":
                continue
            cams.append((int(d.name[5:]), (d / "name").read_text().strip()))
        except (OSError, ValueError):
            continue
    cams.sort(key=lambda c: next((i for i, p in enumerate(preferred) if p.lower() in c[1].lower()), len(preferred)))
    for index, name in cams:
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        ok, frame = False, None
        for _ in range(3):
            ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            continue
        if frame.ndim == 3 and np.array_equal(frame[..., 0], frame[..., 1]) and np.array_equal(frame[..., 1], frame[..., 2]):
            log.debug("camera %d (%s) is grey, infrared: skipped", index, name)
            continue
        log.info("camera: /dev/video%d (%s)", index, name)
        return index
    return None


def facing(face: np.ndarray, max_yaw: float) -> bool:
    """Whether a face (a YuNet row: box, then eyes, nose and mouth corners) looks at the camera: the nose
    halfway between the eyes (yaw: its offset / the eye distance, ~0.1-0.2 looking at it), and between the
    eyes and the mouth rather than above or below (looking up or down)."""
    right_eye, left_eye, nose = face[4:6], face[6:8], face[8:10]
    eyes, mouth = (right_eye + left_eye) / 2, (face[10:12] + face[12:14]) / 2
    yaw = (nose[0] - eyes[0]) / max(1.0, float(np.linalg.norm(left_eye - right_eye)))
    pitch = (nose[1] - eyes[1]) / max(1.0, float(mouth[1] - eyes[1]))
    return abs(yaw) < max_yaw and 0.3 < pitch < 0.8


class Vision:
    def __init__(self, conf: Conf) -> None:
        import cv2
        self.cv2 = cv2
        model = conf.path("FACE_MODEL")
        if not model.exists():
            raise FileNotFoundError(f"no face model {model}, run fetch-models.sh")
        self.detector = cv2.FaceDetectorYN.create(str(model), "", DETECT_SIZE, conf.num("FACE_MIN_SCORE"))
        self.fps = conf.num("CAMERA_FPS")
        self.mirror = -1 if conf.yes("CAMERA_MIRROR") else 1
        self.reach = conf.num("FACE_GAZE")
        self.preferred = conf["PREFERRED_CAMERAS"].split()
        self.face: tuple[float, float] | None = None  # gaze target, -1..1 each way
        self.size = 0.0                               # face width, fraction of the image
        self.seen = 0.0                               # time.monotonic() a face was last there
        self.preview: np.ndarray | None = None        # small RGB frame with the face boxed, when wanted
        self.person: str | None = None                # who the biggest face is, when known
        self.facing = False                           # the biggest face is looking at Hektor
        self.look_yaw = conf.num("LOOK_YAW")
        self.faces = None
        self.set_recognition(conf.yes("FACE_RECOGNITION"), conf)
        self.next_check = 0.0
        self.enrolling: list[np.ndarray] | None = None  # fingerprints collected while someone enrolls
        self.wanted = 0
        self.enrolled = threading.Event()
        self.want_preview = False
        self.stopped = False
        threading.Thread(target=self._run, name="camera", daemon=True).start()

    def stop(self) -> None:
        self.stopped = True

    def set_recognition(self, on: bool, conf: Conf) -> None:
        """Face recognition on (who the face is) or off (just where it is)."""
        if on and not self.faces:
            from .faces import Faces
            try:
                self.faces = Faces(conf)
            except Exception as e:  # no model: just no names
                log.error("no face recognition: %s", e)
        elif not on:
            self.faces, self.person = None, None

    def enroll(self, seconds: float = 5.0, wanted: int = 12) -> list[np.ndarray]:
        """Fingerprints of the biggest face over the next `seconds` (up to `wanted`), for faces.add."""
        if not self.faces:
            return []
        self.enrolled.clear()
        self.enrolling = []
        self.wanted = wanted
        self.enrolled.wait(seconds)
        prints, self.enrolling = self.enrolling, None
        return prints

    def _recognize(self, frame: np.ndarray, face: np.ndarray, new: bool) -> None:
        """Who `face` (a YuNet row, in DETECT_SIZE pixels) is, now and then; and fingerprints to enroll."""
        now = time.monotonic()
        enrolling = self.enrolling
        if not self.faces or (enrolling is None and not new and now < self.next_check):
            return
        # the detector ran on the small picture: SFace wants the face from the full one
        sx, sy = frame.shape[1] / DETECT_SIZE[0], frame.shape[0] / DETECT_SIZE[1]
        row = face.copy()
        row[0:14:2] *= sx
        row[1:14:2] *= sy
        fingerprint = self.faces.fingerprint(frame, row)
        if enrolling is not None:
            enrolling.append(fingerprint)
            if len(enrolling) >= self.wanted:
                self.enrolled.set()
            return
        name, score = self.faces.who(fingerprint)
        if name != self.person:
            log.info("that's %s (similarity %.2f)", name or "someone I don't know", score)
        self.person = name
        self.next_check = now + (RECHECK if name else 0.5)

    def _open(self):
        index = find_camera(self.preferred)
        if index is None:
            return None
        cap = self.cv2.VideoCapture(index, self.cv2.CAP_V4L2)
        cap.set(self.cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(self.cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(self.cv2.CAP_PROP_BUFFERSIZE, 1)  # always the newest frame
        return cap

    def _run(self) -> None:
        cv2 = self.cv2
        cap, had_face = None, False
        while not self.stopped:
            if cap is None:
                cap = self._open()
                if cap is None:
                    log.warning("no camera found, trying again in 10 s")
                    time.sleep(10)
                    continue
            t = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                log.warning("camera stopped giving frames, reopening")
                cap.release()
                cap = None
                time.sleep(2)
                continue
            small = cv2.resize(frame, DETECT_SIZE)
            _, faces = self.detector.detect(small)
            w, h = DETECT_SIZE
            if faces is not None and len(faces):
                biggest = max(faces, key=lambda f: f[2] * f[3])
                self.facing = facing(biggest, self.look_yaw)
                try:
                    self._recognize(frame, biggest, new=not had_face)
                except cv2.error as e:  # a face cut off at the edge, say: try again on the next frame
                    log.debug("face recognition failed: %s", e)
                x, y, fw, fh = biggest[:4]
                cx, cy = (x + fw / 2) / w * 2 - 1, (y + fh / 2) / h * 2 - 1
                # the camera sees you from the front, so your left is its right: mirror it for the gaze
                target = (float(np.clip(-cx * self.mirror * self.reach, -1, 1)),
                          float(np.clip(cy * self.reach, -1, 1)))
                if self.face:  # smooth out detection jitter
                    target = (self.face[0] * 0.4 + target[0] * 0.6, self.face[1] * 0.4 + target[1] * 0.6)
                self.face, self.size, self.seen = target, float(fw / w), time.monotonic()
                if not had_face:
                    log.info("face found (%.0f%% of the picture wide)", self.size * 100)
                had_face = True
            else:
                if had_face and time.monotonic() - self.seen > 1:
                    log.info("face gone")
                    had_face = False
                self.facing = False
                if time.monotonic() - self.seen > 1:
                    self.face = self.person = None
            watched = share.watched("camera")
            if self.want_preview or watched:
                view = small.copy()
                if faces is not None:
                    for f in faces:
                        x, y, fw, fh = map(int, f[:4])
                        cv2.rectangle(view, (x, y), (x + fw, y + fh), (200, 230, 32), 2)
                self.preview = cv2.cvtColor(view, cv2.COLOR_BGR2RGB) if self.want_preview else None
                if watched:
                    ok, jpg = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if ok:
                        share.write("camera", jpg.tobytes())
            else:
                self.preview = None
            if not watched:
                share.remove("camera")
            time.sleep(max(0.0, 1 / self.fps - (time.monotonic() - t)))
        if cap is not None:
            cap.release()
        share.remove("camera")
