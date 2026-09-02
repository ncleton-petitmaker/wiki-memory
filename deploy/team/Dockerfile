FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN addgroup --system wiki-memory && adduser --system --ingroup wiki-memory --home /nonexistent wiki-memory
COPY deploy/team/requirements.lock ./requirements.lock
RUN python -m pip install --require-hashes -r requirements.lock && rm -rf /root/.cache
COPY LICENSE ./LICENSE
COPY src/wiki_memory /opt/wiki-memory/wiki_memory
ENV PYTHONPATH=/opt/wiki-memory
USER wiki-memory
EXPOSE 8787
ENTRYPOINT ["python", "-m", "wiki_memory.cli"]
CMD ["team-serve", "--host", "0.0.0.0", "--port", "8787"]
