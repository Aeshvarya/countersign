# The hosted demo: the shopper, their agent and the merchant's edge on one page.
# Each visitor gets their own in-memory merchant, so nothing is shared or stored.
# With ANTHROPIC_API_KEY set in the host's environment the agent is Claude;
# without it, the scripted stand-in runs and a visit costs nothing.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server,agent]" && useradd --create-home demo
USER demo
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn countersign.web.app:app --host 0.0.0.0 --port ${PORT}"]
