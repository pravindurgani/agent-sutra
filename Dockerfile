# AgentCore Sandbox — Isolated execution environment for LLM-generated code
# Build: docker build -t agentcore-sandbox .
# Used by tools/sandbox.py when DOCKER_ENABLED=true

FROM python:3.11-slim

# System dependencies for compilation and common packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    libjpeg62-turbo-dev \
    libpng-dev \
    libfreetype6-dev \
    zlib1g-dev \
    nodejs \
    npm \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Pre-install common Python packages to minimize auto-install overhead
RUN pip install --no-cache-dir \
    pandas \
    numpy \
    matplotlib \
    seaborn \
    requests \
    beautifulsoup4 \
    Pillow \
    scikit-learn \
    openpyxl \
    xlsxwriter \
    lxml \
    httpx \
    pyyaml \
    python-dateutil \
    scipy \
    sympy \
    polars \
    pyarrow \
    duckdb

WORKDIR /workspace
