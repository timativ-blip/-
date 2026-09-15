FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY precincts.json .
RUN useradd --uid 10001 --create-home poll && mkdir data && chown poll:poll data
USER poll
ENV DATABASE_PATH=/app/data/exit_poll.sqlite3
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
