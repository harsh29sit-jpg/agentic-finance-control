# Hugging Face Space build (context = the Space repo root, which carries
# backend/ verbatim via scripts/deploy_hf_space.sh).
FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uvicorn

COPY backend/ .

ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "python -m uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
