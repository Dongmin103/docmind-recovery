FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch==2.8.0 --index-url ${TORCH_INDEX} && \
    pip install --no-cache-dir transformers==4.57.1 tokenizers==0.22.2 \
      numpy==2.4.6 safetensors==0.8.0 huggingface-hub==0.36.0 einops==0.8.1
WORKDIR /opt/docmind
COPY service.py ./service.py
COPY model/ ./model/
COPY config.json ./config.json
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
    TOKENIZERS_PARALLELISM=false HF_HOME=/tmp/huggingface
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=20 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).read()"
ENTRYPOINT ["python","/opt/docmind/service.py","--config","/opt/docmind/config.json"]
