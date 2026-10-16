FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
RUN useradd --create-home --uid 10001 app
COPY app ./app
COPY prompts ./prompts
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
