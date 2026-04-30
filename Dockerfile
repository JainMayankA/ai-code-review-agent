FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN touch agent/__init__.py github/__init__.py api/__init__.py tests/__init__.py

ENV GITHUB_TOKEN=""
ENV GITHUB_WEBHOOK_SECRET=""
ENV OPENAI_API_KEY=""
ENV OPENAI_MODEL="gpt-4o"
ENV DRY_RUN="false"

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
