# Plain Python image: the app uses the standard library only, so there is nothing to pip install.
FROM python:3.13-alpine

RUN apk add --no-cache tzdata

ENV PYTHONUNBUFFERED=1 \
    WEEKMENU_DATA=/data

WORKDIR /app
COPY app/ /app/

CMD ["python3", "/app/main.py"]
