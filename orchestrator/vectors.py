"""float32 vectors stored as SQLite blobs, compared with cosine similarity in numpy.

At a few thousand vectors a brute-force scan is effectively instant; past ~100k,
switch to the sqlite-vec extension.
"""

from __future__ import annotations

import numpy as np


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cosine_many(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Cosine of one query against each row of matrix."""
    if matrix.size == 0:
        return np.zeros(0, dtype=np.float32)
    q = normalize(query.astype(np.float32))
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    return (matrix @ q) / norms
