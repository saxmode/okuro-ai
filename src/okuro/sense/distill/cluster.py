### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deterministic k-means over session embeddings — numpy only, no new dependency.
# index: imports | defaults | ClusterResult | _normalize | _kmeans_plusplus | kmeans | suggest_k
# AGENT_HEADER_END -->
"""Cluster the corpus map, with numpy and nothing else.

**Why not scikit-learn.** It is installed in this venv, but it is not a
declared dependency — nothing in ``pyproject.toml`` asks for it, so it is
present by transitive accident and could vanish on any reinstall. numpy is the
opposite: three core dependencies require it (``gguf``, ``kokoro-onnx``,
``soundfile``), so it is guaranteed on every install without adding a line.
k-means over ~10 000 unit vectors is forty lines; taking a hard dependency on
a 30 MB library to avoid writing them would be the wrong trade in both
directions.

**Why cosine works as Euclidean here.** Every vector is L2-normalised before
clustering, and on the unit sphere ``||a-b||^2 = 2(1 - cos(a,b))`` — the same
identity ``embed/repair.py`` documents for the vec0 metric. So a Euclidean
k-means on normalised vectors minimises cosine distance, and the clusters
agree with what ``vec_distill_sessions`` returns at query time.

**Deterministic on purpose.** A fixed seed, k-means++ seeding, and a fixed
iteration cap. The pilot's whole job is to produce a number somebody can act
on; a clustering that returns different cluster counts on a re-run turns
"14 clusters" into an anecdote. Re-running this on unchanged input returns
identical labels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_SEED = 20260813
MAX_ITERATIONS = 100
# Below this a cluster is a coincidence, not a population. Used by
# :func:`suggest_k` to keep k proportionate to the sample.
MIN_CLUSTER_TARGET = 12


@dataclass
class ClusterResult:
    """Labels aligned with the input order, plus what the run cost."""

    labels: list[int]
    k: int
    iterations: int
    converged: bool
    sizes: dict[int, int]
    inertia: float


def suggest_k(n: int) -> int:
    """A k proportionate to the sample, so the pilot need not be told one.

    ``sqrt(n/2)`` is the standard rule of thumb, floored at 2 and capped so
    the average cluster holds at least :data:`MIN_CLUSTER_TARGET` members —
    otherwise a 500-session pilot reports 40 clusters of 12 and says nothing
    about the corpus.
    """
    if n < 4:
        return 1
    import math

    k = int(math.sqrt(n / 2.0))
    return max(2, min(k, max(2, n // MIN_CLUSTER_TARGET)))


def _normalize(matrix):
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero vector has no direction; leaving the norm at 0 would divide by
    # zero and poison every distance in the matrix with NaN, which k-means
    # propagates silently into empty clusters.
    norms[norms == 0] = 1.0
    return matrix / norms


def _kmeans_plusplus(matrix, k: int, rng):
    """k-means++ seeding: spread the initial centres by squared distance.

    Random seeding on embedding data reliably picks two centres inside the
    same dense blob and leaves a real cluster unrepresented. ++ costs k passes
    over the matrix and removes that failure mode.
    """
    import numpy as np

    n = matrix.shape[0]
    centres = [matrix[rng.integers(n)]]
    row_sq = (matrix ** 2).sum(axis=1)
    # Running minimum squared distance to the nearest chosen centre. Kept
    # incrementally rather than recomputed against all centres each round:
    # the all-pairs form materialises an (n, k, dim) array, which at the full
    # backlog (10 000 x 14 x 1024 float32) is 573 MB rebuilt on every one of
    # the k rounds. This holds one length-n vector.
    nearest = row_sq - 2 * (matrix @ centres[0]) + float((centres[0] ** 2).sum())
    np.maximum(nearest, 0, out=nearest)
    for _ in range(1, k):
        d2 = nearest
        total = d2.sum()
        if total <= 0:
            # Every remaining point coincides with a centre — no meaningful
            # split is left, so pad with an existing centre and let the Lloyd
            # loop collapse the duplicate.
            chosen = matrix[rng.integers(n)]
        else:
            chosen = matrix[rng.choice(n, p=d2 / total)]
        centres.append(chosen)
        fresh = row_sq - 2 * (matrix @ chosen) + float((chosen ** 2).sum())
        np.maximum(fresh, 0, out=fresh)
        nearest = np.minimum(nearest, fresh)
    return np.array(centres)


def kmeans(vectors, k: int | None = None, seed: int = DEFAULT_SEED) -> ClusterResult:
    """Cluster L2-normalised copies of ``vectors``. Deterministic.

    ``vectors`` is a sequence of equal-length float sequences. Returns labels
    in the SAME ORDER as the input — the caller pairs them back to session ids
    positionally, so nothing here may reorder.
    """
    import numpy as np

    matrix = np.asarray(list(vectors), dtype=np.float32)
    n = matrix.shape[0] if matrix.ndim == 2 else 0
    if n == 0:
        return ClusterResult([], 0, 0, True, {}, 0.0)
    if k is None:
        k = suggest_k(n)
    k = max(1, min(k, n))

    matrix = _normalize(matrix)
    rng = np.random.default_rng(seed)
    centres = _kmeans_plusplus(matrix, k, rng)

    labels = np.zeros(n, dtype=np.int64)
    iterations = 0
    converged = False
    for iterations in range(1, MAX_ITERATIONS + 1):
        # (n, k) squared distances via the expansion, which avoids
        # materialising an (n, k, dim) intermediate — that array is 10k x 14 x
        # 1024 floats on the full backlog and is the difference between 0.5 GB
        # and 0.6 MB.
        d2 = (
            (matrix ** 2).sum(axis=1, keepdims=True)
            - 2 * matrix @ centres.T
            + (centres ** 2).sum(axis=1)
        )
        new_labels = np.argmin(d2, axis=1)
        if iterations > 1 and np.array_equal(new_labels, labels):
            converged = True
            break
        labels = new_labels
        for j in range(k):
            members = matrix[labels == j]
            if len(members):
                centres[j] = members.mean(axis=0)
            # An empty cluster keeps its previous centre rather than being
            # re-seeded: re-seeding from a random point makes the result
            # depend on iteration count, and this function promises the same
            # answer every run.

    d2 = (
        (matrix ** 2).sum(axis=1, keepdims=True)
        - 2 * matrix @ centres.T
        + (centres ** 2).sum(axis=1)
    )
    inertia = float(np.take_along_axis(d2, labels[:, None], axis=1).sum())

    sizes: dict[int, int] = {}
    for label in labels.tolist():
        sizes[label] = sizes.get(label, 0) + 1

    return ClusterResult(
        labels=[int(x) for x in labels.tolist()],
        k=k,
        iterations=iterations,
        converged=converged,
        sizes=sizes,
        inertia=inertia,
    )
