"""Voice clustering: turning embeddings into per-person labels."""

import numpy as np
import pytest

from app.speakers import OnlineSpeakerClusterer, cluster_offline, embedder_status
from app.speakers.embedder import SpeakerEmbedder

RNG = np.random.default_rng(7)


def voice(seed: int, dimensions: int = 64) -> np.ndarray:
    """A stable 'voice': one direction in embedding space."""
    generator = np.random.default_rng(seed)
    vector = generator.normal(size=dimensions).astype(np.float32)
    return vector / np.linalg.norm(vector)


def utterance(base: np.ndarray, jitter: float = 0.08) -> np.ndarray:
    """One utterance from that voice: the direction plus a little noise."""
    noisy = base + RNG.normal(scale=jitter, size=base.shape).astype(np.float32)
    return noisy / np.linalg.norm(noisy)


def test_the_same_voice_keeps_one_label():
    clusterer = OnlineSpeakerClusterer(threshold=0.6)
    base = voice(1)
    labels = [clusterer.assign(utterance(base)) for _ in range(8)]
    assert set(labels) == {"S1"}


def test_two_voices_get_two_labels():
    clusterer = OnlineSpeakerClusterer(threshold=0.6)
    alice, bob = voice(1), voice(2)
    labels = [clusterer.assign(utterance(person)) for person in (alice, bob, alice, bob, alice)]
    assert labels == ["S1", "S2", "S1", "S2", "S1"]
    assert clusterer.speaker_count == 2


def test_the_speaker_cap_is_respected():
    clusterer = OnlineSpeakerClusterer(threshold=0.99, max_speakers=3)
    for index in range(10):
        clusterer.assign(voice(index))
    assert clusterer.speaker_count == 3


def test_a_low_threshold_merges_everyone():
    clusterer = OnlineSpeakerClusterer(threshold=-1.0)
    assert {clusterer.assign(voice(index)) for index in range(5)} == {"S1"}


def test_reset_forgets_the_voices():
    clusterer = OnlineSpeakerClusterer()
    clusterer.assign(voice(1))
    clusterer.reset()
    assert clusterer.speaker_count == 0


def test_offline_clustering_recovers_three_speakers():
    people = [voice(index) for index in (11, 12, 13)]
    vectors = [utterance(people[index % 3]) for index in range(30)]

    labels = cluster_offline(vectors, threshold=0.6)
    assert len(labels) == 30
    assert len(set(labels)) == 3
    # Every third utterance came from the same person.
    for offset in range(3):
        assert len(set(labels[offset::3])) == 1


def test_offline_clustering_fixes_an_early_online_mistake():
    alice, bob = voice(21), voice(22)
    # Alice speaks once, then Bob dominates - an order that can trip up the
    # single-pass online assignment.
    vectors = [utterance(alice)] + [utterance(bob) for _ in range(9)] + [utterance(alice) for _ in range(6)]

    labels = cluster_offline(vectors, threshold=0.6)
    assert labels[0] == labels[-1]
    assert len(set(labels)) == 2


def test_offline_clustering_renumbers_from_first_appearance():
    labels = cluster_offline([utterance(voice(31)), utterance(voice(32))], threshold=0.6)
    assert labels[0] == "S1" and labels[1] == "S2"


def test_offline_clustering_handles_an_empty_meeting():
    assert cluster_offline([]) == []


def test_embedder_status_reports_what_is_installed():
    status = embedder_status("auto")
    assert set(status["installed"]) == {"speechbrain", "resemblyzer"}
    assert status["available"] is (status["backend"] is not None)


def test_the_embedder_degrades_quietly_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr("app.speakers.embedder._speechbrain_available", lambda: False)
    monkeypatch.setattr("app.speakers.embedder._resemblyzer_available", lambda: False)

    embedder = SpeakerEmbedder()
    assert embedder.load() is False
    assert embedder.embed(np.zeros(16_000, dtype=np.float32)) is None


def test_short_clips_are_not_embedded(monkeypatch):
    embedder = SpeakerEmbedder()
    monkeypatch.setattr(embedder, "load", lambda: pytest.fail("should not load for a short clip"))
    assert embedder.embed(np.zeros(1_000, dtype=np.float32)) is None
