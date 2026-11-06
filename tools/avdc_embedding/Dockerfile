# Extend the validated CPU image with its existing local model/tokenizer caches.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY wheels /tmp/ov-wheels
RUN python -m pip install --no-index --find-links /tmp/ov-wheels --no-deps \
    openvino==2026.4.1 openvino-telemetry==2025.2.0 \
    && rm -rf /tmp/ov-wheels
