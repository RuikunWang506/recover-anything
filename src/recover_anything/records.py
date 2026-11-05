"""Shared data types."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Found:
    """One file the engine found (not yet written anywhere)."""

    kind: str                      # 'jpeg', 'mp4', 'png', ...
    ext: str                       # '.jpg', '.mov', ...
    extents: list                  # [(absolute offset, length), ...] in source order
    origin: str                    # 'filesystem' | 'lost-folder' | 'carved' | 'reassembled'
    name: Optional[str] = None     # original file name, if the filesystem kept it
    path: Optional[str] = None     # original folder path on the card
    deleted: bool = True
    fs_time: Optional[_dt.datetime] = None
    status: str = "unverified"     # 'ok' | 'partial' | 'corrupt' | 'unverified'
    note: str = ""
    expected_size: Optional[int] = None
    meta: dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return sum(n for _, n in self.extents)

    @property
    def start(self) -> int:
        return self.extents[0][0] if self.extents else -1

    def read(self, src) -> bytes:
        return b"".join(src.read(o, n) for o, n in self.extents)


@dataclass
class Geometry:
    """Where clusters live inside the source, when a filesystem told us."""

    data_offset: int
    cluster_size: int
    first_cluster: int
    cluster_count: int

    def offset(self, c: int) -> int:
        return self.data_offset + (c - self.first_cluster) * self.cluster_size

    def cluster_at(self, off: int) -> int:
        return self.first_cluster + (off - self.data_offset) // self.cluster_size

    @property
    def last_cluster(self) -> int:
        return self.first_cluster + self.cluster_count - 1
