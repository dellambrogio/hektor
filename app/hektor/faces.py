"""Who's there: face recognition with OpenCV's SFace, on the faces YuNet finds (vision.py).

A known person is a name and a few face fingerprints (128 numbers each), made when they ask Hektor to
(the f key, or "remember me": voice.py asks the name, checks it, takes the picture) and kept in ~/.local/share/hektor/faces.json on this machine: no pictures, and
nobody who didn't ask. A face is a person when it's close enough to one of their fingerprints (cosine
similarity of at least FACE_MATCH). "Forget me" deletes the fingerprints of whoever Hektor sees.
"""
import json
import logging
import os
import re
import threading

import numpy as np

from .config import DATA, Conf

log = logging.getLogger(__name__)
FACES = DATA / "faces.json"
REMEMBER = re.compile(r"\b(?:remember|learn|memori[sz]e)\s+(?:me|my face)\b", re.I)
FORGET = re.compile(r"\bforget\s+(?:me|my face|about me)\b", re.I)
NAME = re.compile(r"\b(?:i'm|i am|my name is|my name's|call me|this is)\s+([^\W\d_][\w'-]*)", re.I)
NOT_NAMES = {"a", "an", "the", "here", "back", "not", "just", "so", "very", "fine", "good", "ok", "okay",
             "hektor", "hector", "your", "you", "me", "uh", "um", "er", "erm", "hmm", "ah", "oh", "well"}


LEAD_IN = re.compile(r"^(?:(?:well|so|ok(?:ay)?|hi|hello|hey|yes|yeah)\W+)*(?:my name is|my name's|i'm|i am|"
                     r"it's|it is|call me|this is|name is|name's)\W+", re.I)
YES = {"yes", "yeah", "yep", "yup", "right", "correct", "exactly", "sure", "ok", "okay", "that's", "true"}
NO = {"no", "nope", "nah", "wrong", "not", "isn't", "incorrect"}


def name_from(text: str) -> str | None:
    """The name in an answer to "what's your name?": "Anna", "It's Anna Maria." → "Anna Maria"."""
    rest = LEAD_IN.sub("", text.strip())
    words = [w for w in re.findall(r"[^\W\d_][\w'-]*", rest) if w.lower() not in NOT_NAMES][:2]
    return " ".join(w[:1].upper() + w[1:] for w in words) or None


def yes_no(text: str | None) -> bool | None:
    """True for yes, False for no, None for neither (or nothing heard)."""
    words = set(re.findall(r"[\w']+", (text or "").lower()))
    if words & NO:  # "that's not right" has a yes word too
        return False
    return True if words & YES else None


def intent(text: str) -> tuple[str, str | None] | None:
    """("remember", the name or None) or ("forget", None) when what was said asks for it, else None."""
    if FORGET.search(text):
        return "forget", None
    if REMEMBER.search(text):
        m = NAME.search(text)
        name = m.group(1).strip("'-") if m else ""
        return "remember", name[:1].upper() + name[1:] if name and name.lower() not in NOT_NAMES else None
    return None


class Faces:
    def __init__(self, conf: Conf) -> None:
        import cv2
        model = conf.path("FACE_RECOGNIZER")
        if not model.exists():
            raise FileNotFoundError(f"no face recognition model {model}, run fetch-models.sh")
        self.recognizer = cv2.FaceRecognizerSF.create(str(model), "")
        self.threshold = conf.num("FACE_MATCH")
        self.lock = threading.Lock()
        self.people: dict[str, np.ndarray] = {}  # name: fingerprints, one per row, unit length
        self.stamp = None
        with self.lock:
            self._load()
        log.info("face recognition on, knows %s", ", ".join(self.people) or "nobody yet")

    def _load(self) -> None:
        """Read faces.json again if it changed (dev/run.sh faces forget, say), with the lock held."""
        try:
            stamp = FACES.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        if stamp == self.stamp:
            return
        self.stamp, self.people = stamp, {}
        if stamp is None:
            return
        try:
            for name, rows in json.loads(FACES.read_text()).items():
                self.people[name] = np.array(rows, dtype=np.float32)
        except (OSError, ValueError) as e:
            log.error("can't read %s: %s", FACES, e)

    def fingerprint(self, frame: np.ndarray, face: np.ndarray) -> np.ndarray:
        """The fingerprint of `face` (a YuNet row, in `frame`'s pixels), unit length."""
        aligned = self.recognizer.alignCrop(frame, face)
        f = self.recognizer.feature(aligned).flatten().astype(np.float32)
        return f / (np.linalg.norm(f) or 1.0)

    def who(self, fingerprint: np.ndarray) -> tuple[str | None, float]:
        """(name, similarity) of the closest person, the name None when nobody's close enough."""
        best, score = None, 0.0
        with self.lock:
            self._load()
            for name, prints in self.people.items():
                s = float(np.max(prints @ fingerprint))
                if s > score:
                    best, score = name, s
        return (best, score) if score >= self.threshold else (None, score)

    def add(self, name: str, prints: list[np.ndarray]) -> None:
        """Remember `name` by these fingerprints (instead of any from before)."""
        with self.lock:
            self._load()
            self.people[name] = np.vstack(prints)
            self._save()
        log.info("remembered %s (%d fingerprints)", name, len(prints))

    def forget(self, name: str) -> None:
        with self.lock:
            self._load()
            self.people.pop(name, None)
            self._save()
        log.info("forgot %s", name)

    def _save(self) -> None:
        FACES.parent.mkdir(parents=True, exist_ok=True)
        tmp = FACES.with_suffix(".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({n: np.round(p, 5).tolist() for n, p in self.people.items()}, f)
        tmp.replace(FACES)
        self.stamp = FACES.stat().st_mtime_ns
