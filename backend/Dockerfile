FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY ["Lambda Functions/CowrieLogParser.py", "/app/CowrieLogParser.py"]
ENV AWS_MODE=local
ENV AWS_REGION=us-east-1
ENV AWS_DEFAULT_REGION=us-east-1
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
