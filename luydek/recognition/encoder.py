"""CLIP image encoder (ViT-B/32), adapted from Desktop/ProductImageSearch.

Product matching compares a crop's embedding with the store's own gallery of
enrolled photos. Adding a product means taking photos, not retraining. Every
vector leaves here L2-normalised, so a dot product is a cosine similarity.
"""

from __future__ import annotations

import threading
from typing import Protocol, Sequence

import numpy as np
from PIL import Image


class Encoder(Protocol):
    dim: int

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray: ...


class ClipEncoder:
    def __init__(self, model_name: str = "openai/clip-vit-base-patch32", device: str = "auto") -> None:
        import torch
        import transformers
        from transformers import CLIPModel, CLIPProcessor

        self._torch = torch
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        # transformers 5 renamed the fast-processor flag; the torchvision-backed
        # path is about twice as fast per image as the PIL one.
        v5 = int(transformers.__version__.split(".")[0]) >= 5
        try:
            self.processor = CLIPProcessor.from_pretrained(
                model_name, **({"backend": "torchvision"} if v5 else {"use_fast": True})
            )
        except (ImportError, ValueError):
            self.processor = CLIPProcessor.from_pretrained(model_name)
        self.model = CLIPModel.from_pretrained(model_name).to(self.device).eval()
        self.dim = int(self.model.config.projection_dim)

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        with self._torch.inference_mode():
            inputs = self.processor(images=[im.convert("RGB") for im in images], return_tensors="pt")
            feats = self.model.get_image_features(pixel_values=inputs["pixel_values"].to(self.device))
            # transformers 5 wraps the projected embedding in an output object.
            feats = getattr(feats, "pooler_output", feats).float()
            feats = feats / feats.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        return feats.cpu().numpy().astype(np.float32)


_lock = threading.Lock()
_encoder: Encoder | None = None


def get_encoder(model_name: str = "openai/clip-vit-base-patch32", device: str = "auto") -> Encoder:
    """The process-wide encoder, loaded once (about 5 s on CPU)."""
    global _encoder
    with _lock:
        if _encoder is None:
            _encoder = ClipEncoder(model_name, device)
        return _encoder


def loaded() -> Encoder | None:
    return _encoder


def set_encoder(encoder: Encoder | None) -> None:
    """Swap the encoder (tests use a cheap stand-in)."""
    global _encoder
    _encoder = encoder
