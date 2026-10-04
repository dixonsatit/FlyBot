# FlyBot MQTT bridge (fly-brain controller + calendar reminders + LLM cortex).
#   docker build -t flybot .
#   docker run --rm -v $PWD/data/gains.json:/config/gains.json:ro -e FLYBOT_HOST=<broker> flybot
# Options come from flags or FLYBOT_<OPTION> environment variables (see flybot/mqtt_bridge.py).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    FLYBOT_GAINS=/config/gains.json FLYBOT_HEARTBEAT=/tmp/flybot-heartbeat \
    HOME=/tmp XDG_CACHE_HOME=/tmp/.cache
WORKDIR /app
COPY pyproject.toml ./
COPY flybot ./flybot
RUN pip install ".[llm,calendar]" && useradd --uid 10001 --no-create-home flybot
# nengo keeps a decoder cache under $XDG_CACHE_HOME; /tmp is the only writable path (emptyDir in K8s)
USER 10001

ENTRYPOINT ["python", "-m", "flybot.mqtt_bridge"]
