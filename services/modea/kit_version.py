"""The kit version this process runs, read from the file the kit ships (ADR-058, decisions 3 and 5).

`docker/KIT_VERSION` carries the number on its first line. The miner image copies it to `/app/built-for/`
(docker/Dockerfile.miner), so inside a container the number is the one the image was built from; a run
from a source tree reads the file of that tree. `DENDRA_KIT_VERSION_FILE` names another file, for a bench.

What the reader answers, and the zero rule. A file that cannot be read, or whose first line is not an
integer, answers None: the daemon says so, and a deposit then carries no `kit_version`, which the relay
refuses once its floor is armed. The absence is loud on the miner's side on purpose; answering 0 would
describe a kit older than every release and hide the defect behind a refusal that looks like age.
"""
from __future__ import annotations

import os

KIT_VERSION_FILE_ENV = "DENDRA_KIT_VERSION_FILE"
IMAGE_FILE = "/app/built-for/KIT_VERSION"
# The field a signed deposit and a capacity report carry; the relay and the registry read it by this name.
FIELD = "kit_version"


def kit_version_file() -> str:
    """The file to read: the environment, then the image's copy, then the source tree's."""
    named = (os.environ.get(KIT_VERSION_FILE_ENV) or "").strip()
    if named:
        return named
    if os.path.exists(IMAGE_FILE):
        return IMAGE_FILE
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "..", "docker", "KIT_VERSION"))


def read_kit_version(path: str | None = None):
    """The integer on the first line of the file, or None when it cannot be read as one."""
    p = path or kit_version_file()
    try:
        with open(p, encoding="utf-8") as f:
            first = f.readline().strip()
    except OSError:
        return None
    if not first.isdigit():
        return None
    return int(first)


def with_kit_version(obj, version):
    """`obj` with the field added when it is an object without one and `version` is known. Anything else
    is returned as it is: a body that is not an object has no place for the field, and a body that already
    declares one is not rewritten."""
    if version is None or not isinstance(obj, dict) or FIELD in obj:
        return obj
    out = dict(obj)
    out[FIELD] = int(version)
    return out
