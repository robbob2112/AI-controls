#!/bin/bash

source /opt/intel/oneapi/setvars.sh >/dev/null 2>&1
export ONEAPI_DEVICE_SELECTOR=level_zero:0
export UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1

exec /scratch/b65-model-testing/llama.cpp/build-sycl/bin/llama-server \
    --models-preset /etc/hal-llama/models.ini \
    --models-max 1 \
    --host 127.0.0.1 \
    --port 11436 \
    --api-prefix /llama \
    -ngl 99 -c 8192 -np 1 -fa on -b 2048 -ub 512 -t 16 \
    --split-mode none \
    --jinja --no-agent --no-ui-mcp-proxy
