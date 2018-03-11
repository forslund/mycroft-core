#! /usr/bin/env bash

echo "Generating token for websocket"
TOKEN=$(python -c "from random import SystemRandom as sr;print('{:X}'.format(sr().getrandbits(64)))")

echo $TOKEN > ~/.mycroft/token
