"""Normalization and activation primitives.

RMSNorm (head-wise / weight-wise), L2Norm and Swish are used verbatim by the
KDA and Gated-MLA layers (kda-formulas.md section 1-2).
"""

from __future__ import annotations

import jax
import jax.numpy as jnp


def swish(x: jnp.ndarray) -> jnp.ndarray:
    """Swish(x) = x * sigmoid(x)."""
    return x * jax.nn.sigmoid(x)


def l2_norm(x: jnp.ndarray, eps: float = 1e-12, axis: int = -1) -> jnp.ndarray:
    """L2-normalise along ``axis`` (used for KDA q/k per head)."""
    return x / (jnp.linalg.norm(x, axis=axis, keepdims=True) + eps)


def rms_norm(x: jnp.ndarray, weight: jnp.ndarray, eps: float = 1e-6) -> jnp.ndarray:
    """Weighted RMSNorm: ``x / sqrt(mean(x^2) + eps) * weight``."""
    ms = jnp.mean(jnp.square(x), axis=-1, keepdims=True)
    return x * jnp.reciprocal(jnp.sqrt(ms + eps)) * weight


def headwise_rms_norm(x: jnp.ndarray, eps: float = 1e-6) -> jnp.ndarray:
    """Unweighted RMSNorm per head (KDA output, Eq. 6)."""
    ms = jnp.mean(jnp.square(x), axis=-1, keepdims=True)
    return x * jnp.reciprocal(jnp.sqrt(ms + eps))
