import torch
from torch import nn
import numpy as np
import cv2
from typing import Any, Dict, List, Optional, Tuple
from ultralytics import YOLO
import math

class FastSAM:
    def __init__(
        self,
        checkpoint: str,    
    ) -> None:
        self.model_path = checkpoint
        self.model = YOLO(self.model_path)

    def to(self, device):
        self.model.to(device)

    @property
    def device(self) -> Any:
        return self.model.device

    def __call__(self, source=None, stream=False, **kwargs):
        return self.model(source=source, stream=stream, **kwargs)

class FastSamAutomaticMaskGenerator:
    def __init__(
        self,
        model: FastSAM,
        points_per_batch: int = None,
    ) -> None:
        self.model = model
        self.points_per_batch = points_per_batch

    def generate(self, image: np.ndarray) -> List[Dict[str, Any]]:
        height, width = image.shape[:2]
        new_height = math.ceil(height / 32) * 32
        new_width = math.ceil(width / 32) * 32
        resize_image = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_LANCZOS4)

        results = self.model(
            source=resize_image,
            stream=False,
            imgsz=max(new_height, new_width),
            device=self.model.device,
            retina_masks=True,
            iou=0.7,
            conf=0.25,
            max_det=256)
        
        annotations = results[0].masks.data

        if isinstance(annotations[0], torch.Tensor):
            annotations = np.array(annotations.cpu())
        
        annotations_list = []
        for i, mask in enumerate(annotations):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_AREA)
            mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
            
            annotations_list.append(dict(segmentation=mask.astype(bool)))

        return annotations_list
