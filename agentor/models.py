from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ItemStatus(str, Enum):
    QUEUED = "queued"
    WORKING = "working"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    CANCELLED = "cancelled"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class Item:
    """A work item extracted from a source file."""
    id: str  # stable hash of source_file + title + body
    title: str
    body: str
    source_file: str  # relative to project root
    source_line: int  # 1-indexed line in source file
    tags: dict[str, str] = field(default_factory=dict)  # @priority:high -> {"priority": "high"}
