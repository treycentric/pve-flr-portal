"""PH.5: splits restore content into `agent/file-write`-sized pieces
(docs/plan.md §7.5). `agent/file-write` is a genuine one-shot call - no
handle/offset parameter exists, so a file bigger than one call's payload
ceiling can't be assembled by repeated calls to the same guest path
(confirmed against Proxmox forum/pvesh examples, see docs/plan.md §7.5).
A single chunk means Design A (quick restore, no guest-exec) is usable;
more than one means Design B's per-chunk-scratch-file + guest-exec
concatenation is required instead.
"""
import base64
from dataclasses import dataclass

# Conservative default until the real per-call ceiling is confirmed
# against a live PVE version (docs/plan.md §7.5's open question: ~40-60
# KiB community reports for the actual pveproxy POST-body-bound figure,
# versus a separately-reported ~48 MB QGA-internal limit that almost
# certainly applies to a different layer).
DEFAULT_CHUNK_SIZE_BYTES = 40 * 1024


@dataclass(frozen=True)
class Chunk:
    index: int
    content_b64: str
    byte_count: int


def split_into_chunks(content: bytes, chunk_size_bytes: int = DEFAULT_CHUNK_SIZE_BYTES) -> list[Chunk]:
    """Splits raw bytes into base64-encoded chunks no larger than
    chunk_size_bytes of *pre-encoding* content each. Empty content yields
    a single empty chunk (still needs one file-write call to create an
    empty file in the guest)."""
    if chunk_size_bytes <= 0:
        raise ValueError("chunk_size_bytes must be positive")
    if not content:
        return [Chunk(index=0, content_b64="", byte_count=0)]
    chunks = []
    for i, start in enumerate(range(0, len(content), chunk_size_bytes)):
        piece = content[start : start + chunk_size_bytes]
        chunks.append(Chunk(index=i, content_b64=base64.b64encode(piece).decode("ascii"), byte_count=len(piece)))
    return chunks


def needs_guest_exec(chunks: list[Chunk]) -> bool:
    """More than one chunk means the pieces have to be written to separate
    guest-side scratch files and reassembled with guest-exec (Design B) -
    a single chunk can go straight to the destination via one file-write
    call (Design A)."""
    return len(chunks) > 1


def scratch_filename(job_id: str, chunk: Chunk) -> str:
    """Deterministic, collision-safe per-chunk scratch filename within a
    job's own scratch directory (docs/plan.md §7.5)."""
    return f"{job_id}.part{chunk.index:05d}"
