"""faces: who Hektor knows by face (app/hektor/faces.py), forgetting and renaming someone. Run it on hektor,
where faces.json is (the monitor app's faces panel does, over SSH); the running app notices the change."""
import json
import os
import re

from hektor.faces import FACES

USAGE = "usage: dev/run.sh faces [forget NAME | rename OLD NEW]"


def save(people: dict) -> None:
    tmp = FACES.with_suffix(".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(people, f)
    tmp.replace(FACES)


def run(args: list[str]) -> None:
    try:
        people = json.loads(FACES.read_text())
    except FileNotFoundError:
        people = {}
    known = ", ".join(people) or "nobody"
    if args[:1] == ["forget"] and len(args) == 2:
        name = args[1]
        if name not in people:
            raise SystemExit(f"Hektor doesn't know {name!r}; it knows: {known}")
        del people[name]
        save(people)
        print(f"forgot {name}")
    elif args[:1] == ["rename"] and len(args) == 3:
        old, new = args[1], " ".join(args[2].split())
        if old not in people:
            raise SystemExit(f"Hektor doesn't know {old!r}; it knows: {known}")
        if not re.fullmatch(r"[^\W\d_][\w' -]{0,39}", new):
            raise SystemExit(f"{new!r} isn't a name (letters, spaces, ' and -, up to 40)")
        if new in people and new != old:
            raise SystemExit(f"Hektor already knows a {new}")
        people = {new if n == old else n: prints for n, prints in people.items()}  # keeps the order
        save(people)
        print(f"renamed {old} to {new}")
    elif args:
        raise SystemExit(USAGE)
    else:
        print("\n".join(f"{name}  ({len(rows)} fingerprints)" for name, rows in people.items())
              or f"nobody yet ({FACES})")
