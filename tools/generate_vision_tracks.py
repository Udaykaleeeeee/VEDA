"""Generate lightweight, browser-friendly detections for VEDA demo videos.

The detector runs once on the local MP4 files.  The web UI then interpolates the
resulting normalized boxes while the video plays, so demo playback needs no GPU
and never uploads footage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = ROOT / "veda" / "models" / "vision" / "yolo11n.onnx"
DEFAULT_POSE_MODEL = ROOT / "veda" / "models" / "vision" / "yolo11n-pose.onnx"
DEFAULT_MEDIA_DIR = ROOT / "veda" / "web" / "staticcams"
MODEL_SOURCE = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.onnx"
POSE_MODEL_SOURCE = (
    "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n-pose.onnx"
)

# The bundled COCO model is trusted only for classes it was trained to detect.
# In particular, VEDA does not relabel generic shapes as pipe progress.
CLASS_NAMES = {
    0: ("worker", "Worker"),
    1: ("bicycle", "Bicycle"),
    2: ("vehicle", "Vehicle"),
    3: ("motorcycle", "Motorcycle"),
    5: ("vehicle", "Bus"),
    7: ("vehicle", "Truck"),
    1001: ("suspended-load", "Suspended hook candidate"),
    1002: ("vehicle", "Parked work vehicle"),
}


@dataclass
class Detection:
    class_id: int
    confidence: float
    box: tuple[float, float, float, float]
    track_id: int = 0
    metadata: dict = field(default_factory=dict)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _letterbox(frame: np.ndarray, size: int = 640) -> tuple[np.ndarray, float, int, int]:
    height, width = frame.shape[:2]
    scale = min(size / width, size / height)
    resized_width, resized_height = int(round(width * scale)), int(round(height * scale))
    resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    pad_x = (size - resized_width) // 2
    pad_y = (size - resized_height) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width] = resized
    return canvas, scale, pad_x, pad_y


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    overlap = max(0.0, right - left) * max(0.0, bottom - top)
    union = aw * ah + bw * bh - overlap
    return overlap / union if union > 0 else 0.0


def _center_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return float(((ax + aw / 2 - bx - bw / 2) ** 2 +
                  (ay + ah / 2 - by - bh / 2) ** 2) ** 0.5)


class SimpleTracker:
    """Small nearest-neighbour tracker sufficient for sampled demo footage."""

    def __init__(self, max_gap: int = 4) -> None:
        self.max_gap = max_gap
        self.next_id = 1
        self.active: dict[int, tuple[int, tuple[float, float, float, float], int]] = {}

    def assign(self, detections: list[Detection], sample_index: int) -> list[Detection]:
        candidates: list[tuple[float, int, int]] = []
        for det_index, detection in enumerate(detections):
            for track_id, (class_id, box, last_seen) in self.active.items():
                if class_id != detection.class_id or sample_index - last_seen > self.max_gap:
                    continue
                overlap = _iou(detection.box, box)
                distance = _center_distance(detection.box, box)
                if overlap >= 0.08 or distance <= 18.0:
                    candidates.append((overlap - distance / 100.0, det_index, track_id))

        used_detections: set[int] = set()
        used_tracks: set[int] = set()
        for _, det_index, track_id in sorted(candidates, reverse=True):
            if det_index in used_detections or track_id in used_tracks:
                continue
            detection = detections[det_index]
            previous = self.active[track_id][1]
            # Smooth detector jitter but keep enough weight on the new frame to
            # visibly follow a walking person.
            detection.box = tuple(round(previous[i] * 0.25 + detection.box[i] * 0.75, 3)
                                  for i in range(4))
            detection.track_id = track_id
            used_detections.add(det_index)
            used_tracks.add(track_id)

        for det_index, detection in enumerate(detections):
            if det_index not in used_detections:
                detection.track_id = self.next_id
                self.next_id += 1
            self.active[detection.track_id] = (detection.class_id, detection.box, sample_index)

        self.active = {track_id: value for track_id, value in self.active.items()
                       if sample_index - value[2] <= self.max_gap}
        return detections


class YoloOnnxDetector:
    def __init__(self, model_path: Path, confidence: float = 0.16,
                 image_size: int = 640) -> None:
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.confidence = confidence
        self.image_size = image_size

    def detect(self, frame: np.ndarray) -> list[Detection]:
        canvas, scale, pad_x, pad_y = _letterbox(frame, self.image_size)
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 255.0,
                                     (self.image_size, self.image_size),
                                     swapRB=True, crop=False)
        self.net.setInput(blob)
        output = np.asarray(self.net.forward())
        if output.ndim == 3:
            output = output[0]
        if output.shape[0] < output.shape[1]:
            output = output.T

        height, width = frame.shape[:2]
        boxes: list[list[int]] = []
        scores: list[float] = []
        class_ids: list[int] = []
        for row in output:
            class_scores = row[4:]
            class_id = int(np.argmax(class_scores))
            score = float(class_scores[class_id])
            if class_id not in CLASS_NAMES or score < self.confidence:
                continue
            cx, cy, box_width, box_height = (float(value) for value in row[:4])
            left = (cx - box_width / 2 - pad_x) / scale
            top = (cy - box_height / 2 - pad_y) / scale
            right = (cx + box_width / 2 - pad_x) / scale
            bottom = (cy + box_height / 2 - pad_y) / scale
            left, top = max(0.0, left), max(0.0, top)
            right, bottom = min(float(width), right), min(float(height), bottom)
            if right <= left or bottom <= top:
                continue
            boxes.append([int(round(left)), int(round(top)),
                          int(round(right - left)), int(round(bottom - top))])
            scores.append(score)
            class_ids.append(class_id)

        indices = cv2.dnn.NMSBoxes(boxes, scores, self.confidence, 0.45)
        detections: list[Detection] = []
        for index in np.asarray(indices).reshape(-1).tolist() if len(indices) else []:
            left, top, box_width, box_height = boxes[int(index)]
            detections.append(Detection(
                class_id=class_ids[int(index)],
                confidence=scores[int(index)],
                box=(round(left / width * 100, 3), round(top / height * 100, 3),
                     round(box_width / width * 100, 3), round(box_height / height * 100, 3)),
            ))
        return detections


class PoseOnnxDetector:
    """Decode the official YOLO11 pose export without requiring PyTorch.

    Pose boxes are materially more stable than generic COCO person boxes when a
    worker turns, crouches or bends.  Keypoints are retained only as aggregate
    posture signals; the browser sidecar never stores an image or a biometric.
    """

    def __init__(self, model_path: Path, confidence: float = 0.14,
                 image_size: int = 640) -> None:
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.confidence = confidence
        self.image_size = image_size

    def detect(self, frame: np.ndarray) -> list[Detection]:
        canvas, scale, pad_x, pad_y = _letterbox(frame, self.image_size)
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 255.0,
                                     (self.image_size, self.image_size),
                                     swapRB=True, crop=False)
        self.net.setInput(blob)
        output = np.asarray(self.net.forward())
        if output.ndim == 3:
            output = output[0]
        if output.shape[0] < output.shape[1]:
            output = output.T

        height, width = frame.shape[:2]
        boxes: list[list[int]] = []
        scores: list[float] = []
        metadata: list[dict] = []
        for row in output:
            score = float(row[4])
            if score < self.confidence:
                continue
            cx, cy, box_width, box_height = (float(value) for value in row[:4])
            left = max(0.0, (cx - box_width / 2 - pad_x) / scale)
            top = max(0.0, (cy - box_height / 2 - pad_y) / scale)
            right = min(float(width), (cx + box_width / 2 - pad_x) / scale)
            bottom = min(float(height), (cy + box_height / 2 - pad_y) / scale)
            if right <= left or bottom <= top:
                continue
            # Pad the visible outline slightly so a bent worker is not reduced
            # to a tiny torso box.  Values remain clamped to the source frame.
            pad_w = (right - left) * 0.07
            pad_h = (bottom - top) * 0.04
            left, right = max(0.0, left - pad_w), min(float(width), right + pad_w)
            top, bottom = max(0.0, top - pad_h), min(float(height), bottom + pad_h)
            boxes.append([int(round(left)), int(round(top)),
                          int(round(right - left)), int(round(bottom - top))])
            scores.append(score)
            keypoints = row[5:].reshape(-1, 3) if len(row) >= 56 else np.empty((0, 3))
            visible = int(np.sum(keypoints[:, 2] >= 0.35)) if len(keypoints) else 0
            metadata.append({
                "basis": "pose",
                "visible_keypoints": visible,
                "posture_ratio": round((right - left) / max(1.0, bottom - top), 4),
            })

        indices = cv2.dnn.NMSBoxes(boxes, scores, self.confidence, 0.45)
        detections: list[Detection] = []
        for index in np.asarray(indices).reshape(-1).tolist() if len(indices) else []:
            left, top, box_width, box_height = boxes[int(index)]
            detections.append(Detection(
                class_id=0,
                confidence=scores[int(index)],
                box=(round(left / width * 100, 3), round(top / height * 100, 3),
                     round(box_width / width * 100, 3),
                     round(box_height / height * 100, 3)),
                metadata=metadata[int(index)],
            ))
        return detections


def _camera_context(video_name: str, detections: list[Detection]) -> list[Detection]:
    """Apply explicit fixed-camera calibration without inventing object classes."""
    if video_name != "CCTV_2.mp4":
        return detections
    for detection in detections:
        x, y, box_width, box_height = detection.box
        center_y = y + box_height / 2
        # CAM-03's far-left lifting corridor contains the suspended hook.  COCO
        # has no hook class, so pose can otherwise hallucinate it as a person.
        # Keep the result a question/candidate until a reviewer confirms it.
        if (detection.class_id == 0 and x + box_width <= 25 and
                22 <= center_y <= 72 and box_width <= 14 and box_height <= 28):
            detection.class_id = 1001
            detection.metadata.update({
                "basis": "camera_calibrated_lifting_corridor",
                "review_required": True,
                "original_model_label": "person",
            })
    return detections


def _combine_detections(video_name: str, objects: list[Detection],
                        poses: list[Detection]) -> list[Detection]:
    # Pose provides worker boxes.  The generic model remains responsible for
    # road vehicles and never supplies a second, conflicting person outline.
    combined = [item for item in objects if item.class_id != 0] + poses
    return _camera_context(video_name, combined)


def _add_stationary_scene_memory(video_name: str, frames: list[dict]) -> int:
    """Persist repeatedly confirmed static vehicles in a fixed-camera scene."""
    if video_name != "CCTV_2.mp4":
        return 0
    observations = []
    for frame in frames:
        for item in frame["detections"]:
            if (item["class"] == "vehicle" and item["w"] >= 20 and
                    item["h"] >= 10 and item["y"] <= 20):
                observations.append(item)
    if len(observations) < 3:
        return 0
    anchor = {
        key: round(float(np.median([item[key] for item in observations])), 3)
        for key in ("x", "y", "w", "h")
    }
    detector_confidence = float(np.median([item["confidence"] for item in observations]))
    scene_confidence = min(0.94, 0.68 + len(observations) * 0.025)
    for frame in frames:
        # Suppress small vehicle fragments inside the full parked-vehicle box.
        frame["detections"] = [item for item in frame["detections"]
                               if not (item["class"] == "vehicle" and
                                       _iou((item["x"], item["y"], item["w"], item["h"]),
                                            (anchor["x"], anchor["y"], anchor["w"], anchor["h"])) > .08)]
        frame["detections"].append({
            "id": 9001, "class_id": 1002, "class": "vehicle",
            "label": "Parked work vehicle", "confidence": round(scene_confidence, 4),
            **anchor, "basis": "fixed_camera_scene_memory", "state": "scene_memory",
            "detector_confidence": round(detector_confidence, 4),
            "supporting_observations": len(observations),
        })
    return len(observations)


def _temporal_safety_events(video_name: str, frames: list[dict]) -> list[dict]:
    """Create review candidates from sudden worker posture changes over time."""
    if video_name != "CCTV_2.mp4":
        return []
    history: list[tuple[float, dict]] = []
    candidates: list[dict] = []
    for frame in frames:
        timestamp = float(frame["t"])
        for item in frame["detections"]:
            if item["class"] != "worker" or item.get("basis") != "pose":
                continue
            ratio = float(item.get("posture_ratio") or 0)
            center_x = item["x"] + item["w"] / 2
            center_y = item["y"] + item["h"] / 2
            nearby = [(prior_t, prior) for prior_t, prior in history
                      if prior.get("id") == item.get("id") and timestamp - prior_t <= 1.8 and
                      _center_distance((item["x"], item["y"], item["w"], item["h"]),
                                       (prior["x"], prior["y"], prior["w"], prior["h"])) <= 18]
            if (32 <= center_x <= 70 and 34 <= center_y <= 88 and item["h"] >= 18 and
                    ratio >= .74 and nearby):
                prior_t, prior = min(nearby, key=lambda pair: float(pair[1].get("posture_ratio") or 9))
                prior_ratio = float(prior.get("posture_ratio") or 0)
                delta = ratio - prior_ratio
                # A duck/collapse moves the upper edge down while the lower edge
                # stays near the ground.  Requiring that direction rejects a
                # sideways worker or someone simply entering the frame.
                vertical_drop = float(item["y"]) - float(prior["y"])
                if prior_ratio <= .58 and delta >= .18 and vertical_drop >= 7:
                    score = min(.89, .59 + delta * .26 + float(item["confidence"]) * .11)
                    candidates.append({
                        "t": timestamp, "score": score, "track_id": item["id"],
                        "ratio_delta": delta, "prior_t": prior_t,
                    })
            history.append((timestamp, item))
        history = [(prior_t, prior) for prior_t, prior in history
                   if timestamp - prior_t <= 2.0]
    if not candidates:
        return []
    # Deduplicate posture changes that belong to the same motion and retain the
    # strongest explainable candidate in each six-second review window.
    selected: list[dict] = []
    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        if all(abs(candidate["t"] - kept["t"]) > 6 for kept in selected):
            selected.append(candidate)
    selected.sort(key=lambda item: item["t"])
    return [{
        "id": f"cam03-safety-{index + 1}",
        "type": "potential_struck_by_near_miss",
        "severity": "high",
        "label": "Potential struck-by / near-miss",
        "t": round(item["t"], 3),
        "start": round(max(0.0, item["prior_t"] - .75), 3),
        "end": round(item["t"] + 2.0, 3),
        "confidence": round(item["score"], 4),
        "status": "needs_supervisor_review",
        "track_ids": [item["track_id"]],
        "signals": [
            "rapid worker posture change across consecutive frames",
            "worker remained inside the calibrated material-handling workfront",
        ],
        "disclaimer": "Assistive computer-vision alert; not a confirmed incident.",
    } for index, item in enumerate(selected)]


def generate(video_path: Path, output_path: Path, detector: YoloOnnxDetector,
             pose_detector: PoseOnnxDetector, model_path: Path, pose_model_path: Path,
             sample_fps: float) -> dict:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open {video_path}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration = frame_count / fps if fps else 0.0
    interval = max(1, int(round(fps / sample_fps)))
    tracker = SimpleTracker(max_gap=max(3, int(round(sample_fps * 1.5))))
    frames: list[dict] = []
    sample_index = 0
    frame_index = 0
    class_counts: dict[str, int] = {}
    best_time, best_score = 0.0, -1.0

    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index % interval == 0:
            detections = tracker.assign(_combine_detections(
                video_path.name, detector.detect(frame), pose_detector.detect(frame)), sample_index)
            encoded: list[dict] = []
            score = 0.0
            for detection in detections:
                css_class, label = CLASS_NAMES[detection.class_id]
                class_counts[label] = class_counts.get(label, 0) + 1
                score += detection.confidence + (0.35 if detection.class_id == 0 else 0.0)
                x, y, box_width, box_height = detection.box
                encoded.append({
                    "id": detection.track_id,
                    "class_id": detection.class_id,
                    "class": css_class,
                    "label": label,
                    "confidence": round(detection.confidence, 4),
                    "x": x, "y": y, "w": box_width, "h": box_height,
                    **detection.metadata,
                })
            timestamp = round(frame_index / fps, 3)
            frames.append({"t": timestamp, "detections": encoded})
            if score > best_score:
                best_time, best_score = timestamp, score
            sample_index += 1
            if sample_index % 100 == 0:
                print(f"  {video_path.name}: {timestamp:.1f}/{duration:.1f}s", flush=True)
        frame_index += 1
    capture.release()

    scene_observations = _add_stationary_scene_memory(video_path.name, frames)
    events = _temporal_safety_events(video_path.name, frames)

    payload = {
        "schema": "veda.vision.tracks.v1",
        "source": video_path.name,
        "generated_from_local_media": True,
        "model": {
            "name": "Local vision ensemble · pose + objects + temporal safety",
            "sources": [MODEL_SOURCE, POSE_MODEL_SOURCE],
            "sha256": {"objects": _sha256(model_path), "pose": _sha256(pose_model_path)},
            "runtime": f"OpenCV DNN {cv2.__version__}",
            "confidence_thresholds": {"objects": detector.confidence,
                                      "pose": pose_detector.confidence},
            "supported_labels": ["Worker", "road vehicles"],
            "camera_calibrated_candidates": ["Suspended hook candidate"]
                if video_path.name == "CCTV_2.mp4" else [],
        },
        "video": {"width": width, "height": height, "fps": round(fps, 4),
                  "duration": round(duration, 3)},
        "sample_fps": round(fps / interval, 4),
        "highlight_time": best_time,
        "class_samples": class_counts,
        "scene_memory_observations": scene_observations,
        "events": events,
        "frames": frames,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return payload


def _videos(media_dir: Path, requested: Iterable[str]) -> list[Path]:
    paths = [media_dir / name for name in requested]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing local demo video(s): " + ", ".join(missing))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--pose-model", type=Path, default=DEFAULT_POSE_MODEL)
    parser.add_argument("--media-dir", type=Path, default=DEFAULT_MEDIA_DIR)
    parser.add_argument("--sample-fps", type=float, default=2.0)
    parser.add_argument("--confidence", type=float, default=0.16)
    parser.add_argument("videos", nargs="*",
                        default=["CCTV_1.mp4", "CCTV_2.mp4", "LiveCamera_1.mp4"])
    args = parser.parse_args()
    if not args.model.exists():
        raise FileNotFoundError(
            f"Model not found: {args.model}\nDownload the official ONNX asset from {MODEL_SOURCE}")
    if not args.pose_model.exists():
        raise FileNotFoundError(
            f"Pose model not found: {args.pose_model}\n"
            f"Download the official ONNX asset from {POSE_MODEL_SOURCE}")
    detector = YoloOnnxDetector(args.model, confidence=args.confidence)
    pose_detector = PoseOnnxDetector(args.pose_model, confidence=max(.12, args.confidence - .02))
    for video_path in _videos(args.media_dir, args.videos):
        effective_fps = max(args.sample_fps, 4.0) if video_path.name == "CCTV_2.mp4" else args.sample_fps
        print(f"Scanning {video_path.name} with object + pose ensemble…", flush=True)
        output_path = args.media_dir / "detections" / f"{video_path.stem}.json"
        payload = generate(video_path, output_path, detector, pose_detector, args.model,
                           args.pose_model, effective_fps)
        print(f"  wrote {output_path.relative_to(ROOT)} · {len(payload['frames'])} samples · "
              f"highlight {payload['highlight_time']:.1f}s · {payload['class_samples']}")


if __name__ == "__main__":
    main()
