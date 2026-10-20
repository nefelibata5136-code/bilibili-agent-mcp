"""Trusted, immutable per-process identity; never supplied by an MCP caller.

Start separate processes with separate state directories for separate agents.
Set BILI_LIFE_ID and BILI_OWNER_UID before launch. Missing owner permits public
reads only; it never adopts whichever account happens to be logged in.
"""
from dataclasses import dataclass
import os
import re
from typing import Mapping


@dataclass(frozen=True)
class ProcessBinding:
    life_id: str = "local-agent"
    owner_uid: str | None = None

    def __post_init__(self):
        if not isinstance(self.life_id, str) or not self.life_id.strip() or len(self.life_id) > 200:
            raise ValueError("BILI_LIFE_ID_INVALID")
        if self.owner_uid is not None:
            if (not isinstance(self.owner_uid, str)
                    or not re.fullmatch(r"[0-9]{1,19}", self.owner_uid)
                    or not 1 <= int(self.owner_uid) <= 2**63 - 1):
                raise ValueError("BILI_OWNER_UID_INVALID")
            object.__setattr__(self, "owner_uid", str(int(self.owner_uid)))


def from_environment(environment: Mapping[str, str]) -> ProcessBinding:
    """Parse launch configuration; caller changes later do not update binding."""
    return ProcessBinding(environment.get("BILI_LIFE_ID", "local-agent"),
        environment.get("BILI_OWNER_UID") or None)


PROCESS_BINDING = from_environment(os.environ)
