FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install dependencies first (layer cache)
COPY pyproject.toml .
RUN python -c "import tomllib; f=open('pyproject.toml','rb'); d=tomllib.load(f); print('\n'.join(d['project']['dependencies']))" > requirements.txt && \
    uv pip install --system --no-cache -r requirements.txt

# Copy source
COPY . .

CMD ["python", "main.py"]
