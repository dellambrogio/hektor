#!/bin/bash
# Download the speech models Hektor needs into ~/.local/share/hektor (~250 MB). Safe to re-run.
#   whisper  speech to text (whisper.cpp ggml model)
#   piper    voices for speaking
#   vosk     small English model for the "hey hector" wake word
#   yunet    face detector for the camera (OpenCV zoo)
#   sface    face recognition, who the face is (OpenCV zoo)
# Pick a different voice or whisper model with: fetch-models.sh VOICE WHISPER_MODEL
set -e
cd "$(dirname "$0")"
DATA=~/.local/share/hektor
VOICE=${1:-en_US-john-medium}
WHISPER=${2:-ggml-base.bin}
VOSK=vosk-model-small-en-us-0.15
YUNET=face_detection_yunet_2023mar.onnx
SFACE=face_recognition_sface_2021dec.onnx
mkdir -p "$DATA/voices" "$DATA/models"

if [[ ! -f "$DATA/models/$WHISPER" ]]; then
    curl -L --fail -o "$DATA/models/$WHISPER.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER"
    mv "$DATA/models/$WHISPER.part" "$DATA/models/$WHISPER"
fi

if [[ ! -f "$DATA/voices/$VOICE.onnx" ]]; then
    [[ -x .venv/bin/python ]] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
    .venv/bin/python -m piper.download_voices --download-dir "$DATA/voices" "$VOICE"
fi

if [[ ! -d "$DATA/models/$VOSK" ]]; then
    tmp=$(mktemp -d)
    curl -L --fail -o "$tmp/$VOSK.zip" "https://alphacephei.com/vosk/models/$VOSK.zip"
    unzip -q -d "$DATA/models" "$tmp/$VOSK.zip"
    rm -r "$tmp"
fi
if [[ ! -f "$DATA/models/$YUNET" ]]; then
    curl -L --fail -o "$DATA/models/$YUNET.part" \
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/$YUNET"
    mv "$DATA/models/$YUNET.part" "$DATA/models/$YUNET"
fi

if [[ ! -f "$DATA/models/$SFACE" ]]; then
    curl -L --fail -o "$DATA/models/$SFACE.part" \
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/$SFACE"
    mv "$DATA/models/$SFACE.part" "$DATA/models/$SFACE"
fi

echo "models in $DATA:"; ls "$DATA/voices" "$DATA/models"
