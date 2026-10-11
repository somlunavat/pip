"""The preference vector: what kinds of change are worth interrupting you for.

Each Useful / Not useful reaction on an alert pulls the vector toward (or away
from) that change's embedding. Older reactions decay exponentially, so the vector
follows what you care about now. No fine-tuning, no labels to collect.

The vector is recomputed from the stored reaction history each time, which keeps
it exact under decay and is cheap at the scale of a few hundred reactions.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np

from orchestrator.db import Database, utcnow
from orchestrator.vectors import cosine, from_blob, normalize, to_blob

HALF_LIFE_DAYS = 7.0
NEGATIVE_WEIGHT = 0.7  # "Not useful" pushes away a little less hard than "Useful" pulls


class Preferences:
    def __init__(self, db: Database, user_id: str = "me", half_life_days: float = HALF_LIFE_DAYS):
        self.db = db
        self.user_id = user_id
        self.half_life_days = half_life_days

    def _row(self) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM preferences WHERE user_id = ?", (self.user_id,))
        if row is None:
            return {"vector": None, "reactions": []}
        return {"vector": from_blob(row["vector"]) if row["vector"] else None,
                "reactions": json.loads(row["reactions"])}

    def vector(self) -> np.ndarray | None:
        return self._row()["vector"]

    def reactions(self) -> list[dict[str, Any]]:
        return self._row()["reactions"]

    def score(self, embedding: np.ndarray) -> float | None:
        """Cosine against the preference vector; None means no reactions yet (cold start)."""
        vec = self.vector()
        return None if vec is None else cosine(vec, embedding)

    def react(self, change_id: str, useful: bool, now: datetime | None = None) -> np.ndarray | None:
        reactions = [r for r in self.reactions() if r["change_id"] != change_id]
        reactions.append({"change_id": change_id, "useful": useful,
                          "ts": (now or datetime.now(UTC)).isoformat()})
        vec = self._compute(reactions, now or datetime.now(UTC))
        self.db.execute(
            """INSERT INTO preferences (user_id, vector, updated_at, reactions)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET vector = excluded.vector,
                 updated_at = excluded.updated_at, reactions = excluded.reactions""",
            (self.user_id, to_blob(vec) if vec is not None else None, utcnow(),
             json.dumps(reactions)),
        )
        return vec

    def _compute(self, reactions: list[dict[str, Any]], now: datetime) -> np.ndarray | None:
        ids = [r["change_id"] for r in reactions]
        if not ids:
            return None
        marks = ",".join("?" * len(ids))
        rows = self.db.all(
            f"SELECT id, embedding FROM change_events WHERE id IN ({marks})", ids
        )
        embeddings = {r["id"]: from_blob(r["embedding"]) for r in rows if r["embedding"]}
        total: np.ndarray | None = None
        for r in reactions:
            emb = embeddings.get(r["change_id"])
            if emb is None:
                continue
            age_days = (now - datetime.fromisoformat(r["ts"])).total_seconds() / 86400
            weight = math.pow(0.5, max(age_days, 0) / self.half_life_days)
            sign = 1.0 if r["useful"] else -NEGATIVE_WEIGHT
            contrib = sign * weight * normalize(emb)
            total = contrib if total is None else total + contrib
        return None if total is None else normalize(total).astype(np.float32)
