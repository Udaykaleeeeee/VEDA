# Local vision model

VEDA's demo track generator expects `yolo11n.onnx` and
`yolo11n-pose.onnx` in this directory. Both official Ultralytics exports are
kept out of Git. They run through OpenCV DNN, so playback does not require
PyTorch, the Ultralytics Python package, a GPU, or a continuously running
inference service.

Source:
`https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.onnx`

`https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n-pose.onnx`

Regenerate the local detection sidecars after replacing a demo MP4:

```powershell
.venv\Scripts\python.exe tools\generate_vision_tracks.py
```

The pose export supplies more stable worker boxes during turns, crouches and
bending. The COCO object export supplies road-vehicle detections. CAM-03 also
uses an explicit fixed-camera lifting-corridor calibration to turn a person-like
false positive into a **suspended-hook candidate** that requires supervisor
review. It is not a trained hook detector.

A production hook/tower-crane/vehicle model should be fine-tuned against a
governed construction dataset such as ExtCon
(`https://github.com/dyxm/ExtCon`), then validated on the actual camera geometry
before deployment. VEDA does not claim that these models detect
pipe installation, measure progress, or confirm an accident; those remain
human-reviewed evidence.
