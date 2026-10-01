FROM python:3.13.7-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY sync.py /app/sync.py
USER 1000:1000
ENTRYPOINT ["python", "/app/sync.py"]
CMD ["watch"]
