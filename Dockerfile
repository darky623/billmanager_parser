FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install dependencies first (layer cache)
COPY pyproject.toml .
RUN uv pip install --system --no-cache $(python -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(' '.join(data['project']['dependencies']))
")

# Copy source
COPY . .

CMD ["python", "main.py"]
