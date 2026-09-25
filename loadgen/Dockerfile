FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY loadgen.py .

RUN useradd --uid 1001 --create-home --shell /bin/false loadgen
USER 1001

ENTRYPOINT ["python", "-u", "loadgen.py"]
