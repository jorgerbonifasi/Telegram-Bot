FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Railway injects PORT; bot.py uses it for the token upload server
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
