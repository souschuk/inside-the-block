# Sender, analysis and tests. Holds no keys: secrets arrive only through the volume of the `live` service.
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt
WORKDIR /app
COPY pyproject.toml targets.json targets-P.json ./
COPY tiepairs ./tiepairs
COPY tests ./tests
COPY hypotheses ./hypotheses
COPY tools ./tools
COPY frozen ./frozen
COPY data ./data
COPY report ./report
ENTRYPOINT ["python", "-m", "tiepairs"]
