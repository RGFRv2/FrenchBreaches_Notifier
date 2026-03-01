FROM python:3.13-alpine

ENV TZ=Europe/Paris
RUN apk add --no-cache tzdata

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

VOLUME ["/app/data"]

CMD ["python", "-u", "main.py"]
