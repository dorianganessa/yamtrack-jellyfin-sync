FROM python:3.13.18-slim-bookworm@sha256:93498f36dc7e24ef233bfdf6cc17d4b8bd32d94ee737143e906460d5f5a914c5
RUN python -m pip uninstall -y pip \
 && python -c "import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)"
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY sync.py /app/sync.py
USER 1000:1000
ENTRYPOINT ["python", "/app/sync.py"]
CMD ["watch"]
