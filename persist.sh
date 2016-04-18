#!/bin/bash

while true; do
    python `pwd`/readease.py >>out.log 2>>err.log
    echo "Server exited with code %?; restarting..." >>err.log
    sleep 5
done