#! /usr/bin/env bash

ARCH=$(uname -i)

echo "Checking if precise needs to be downloaded"
# Get the latest release for this architecture
LATEST_URL=$(curl -L "https://raw.githubusercontent.com/MycroftAI/precise-data/dist/${ARCH}/latest") > /dev/null || exit 1



PRECISE_TGZ_FILENAME=$(basename "$LATEST_URL")
PRECISE_LOCATION="$HOME/.mycroft/precise"
PRECISE_DL_DEST="$PRECISE_LOCATION/$PRECISE_TGZ_FILENAME"

if [[ ! -f $PRECISE_DL_DEST ]]; then
    echo "Downloading precise"
    curl -L "$LATEST_URL" -o "$PRECISE_DL_DEST"
    tar -xzf $PRECISE_DL_DEST -C $PRECISE_LOCATION
else
    echo "Precise already installed"
fi

# Install Hey Mycroft Model
MODEL_URL="https://github.com/MycroftAI/precise-data/raw/models/hey-mycroft.tar.gz"
MODEL_TGZ_FILENAME=$(basename "$MODEL_URL")
MODEL_DL_DEST="$PRECISE_LOCATION/$MODEL_TGZ_FILENAME"

if [[ ! -f $MODEL_DL_DEST ]]; then
    echo "Downloading Hey Mycroft Model..."
    curl -L "$MODEL_URL" -o "$MODEL_DL_DEST"
    tar -xzf "$MODEL_DL_DEST" -C "$PRECISE_LOCATION"
fi
