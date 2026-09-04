"""Grouping utterances by voice.

Online, during the meeting: each utterance's embedding is compared to the
running centroid of every voice heard so far. Close enough to one, it joins it;
otherwise it starts a new speaker. That gives labels while the meeting is still
running, at the cost of being order-dependent - an early mistake sticks.

Offline, afterwards: the stored embeddings are re-clustered from scratch with a
couple of refinement passes, which fixes most of those early mistakes. This is
what the "Re-detect speakers" button runs, and it works from the saved vectors
so it needs no audio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np


def label_for(index: int) -> str:
    return f"S{index + 1}"


def _normalise(vector: Sequence[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(array))
    return array / norm if norm > 0 else array


@dataclass
class OnlineSpeakerClusterer:
    """Assigns a speaker label to each utterance as it arrives."""

    threshold: float = 0.62
    max_speakers: int = 12
    _centroids: list[np.ndarray] = field(default_factory=list)
    _counts: list[int] = field(default_factory=list)

    @property
    def speaker_count(self) -> int:
        return len(self._centroids)

    def assign(self, embedding: Sequence[float] | np.ndarray) -> str:
        vector = _normalise(embedding)
        if vector.size == 0:
            return label_for(0)

        if self._centroids:
            similarities = np.array([float(np.dot(centroid, vector)) for centroid in self._centroids])
            best = int(np.argmax(similarities))
            if similarities[best] >= self.threshold or len(self._centroids) >= self.max_speakers:
                self._merge(best, vector)
                return label_for(best)

        self._centroids.append(vector)
        self._counts.append(1)
        return label_for(len(self._centroids) - 1)

    def _merge(self, index: int, vector: np.ndarray) -> None:
        count = self._counts[index]
        blended = (self._centroids[index] * count + vector) / (count + 1)
        self._centroids[index] = _normalise(blended)
        self._counts[index] = count + 1

    def reset(self) -> None:
        self._centroids.clear()
        self._counts.clear()


def cluster_offline(
    vectors: Sequence[Sequence[float]],
    threshold: float = 0.62,
    max_speakers: int = 12,
    passes: int = 3,
) -> list[str]:
    """Re-cluster a whole meeting. Returns one label per input vector."""
    if not vectors:
        return []

    matrix = np.vstack([_normalise(vector) for vector in vectors])

    # Seed with the same greedy pass, then refine: reassign everything to the
    # nearest centroid and recompute. Two or three rounds is enough to settle.
    seeder = OnlineSpeakerClusterer(threshold=threshold, max_speakers=max_speakers)
    labels = [seeder.assign(row) for row in matrix]
    indices = np.array([int(label[1:]) - 1 for label in labels])

    for _ in range(max(0, passes)):
        unique = sorted(set(indices.tolist()))
        centroids = np.vstack([_normalise(matrix[indices == cluster].mean(axis=0)) for cluster in unique])
        similarities = matrix @ centroids.T
        nearest = similarities.argmax(axis=1)
        updated = np.array([unique[position] for position in nearest])
        if np.array_equal(updated, indices):
            break
        indices = updated

    # Renumber so labels are S1..Sn in order of first appearance.
    order: dict[int, int] = {}
    for cluster in indices.tolist():
        if cluster not in order:
            order[cluster] = len(order)
    return [label_for(order[cluster]) for cluster in indices.tolist()]
