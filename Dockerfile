FROM python:3.11-slim

WORKDIR /app

# 安装系统运行依赖：
# - gcc：部分 Python 包在无 wheel 时需要编译
# - ghostscript：PDF 压缩优先使用
# - libreoffice-writer / libreoffice-impress：Word/PPT 转 PDF
# - fontconfig + 中文字体：发票合并、证件照排版渲染中文水印和页码
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ghostscript \
    libreoffice-writer \
    libreoffice-impress \
    fontconfig \
    fonts-noto-cjk \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create temp directory for file processing
RUN mkdir -p /app/temp

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
