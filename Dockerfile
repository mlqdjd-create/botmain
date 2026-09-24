FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
RUN mkdir -p /app/data

# خطوط عربية + أدوات X
RUN apt-get update && apt-get install -y \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "bot.py"]
