FROM anasty17/mltb:latest

WORKDIR /app
RUN chmod 777 /app

RUN python3 -m venv mltbenv

COPY requirements.txt .
RUN mltbenv/bin/pip install --no-cache-dir -r requirements.txt
RUN pip install playwright --break-system-packages && \
    playwright install chromium && \
    playwright install-deps chromium

COPY . .

RUN sed -i 's/\r$//' *.sh

CMD ["bash", "start.sh"]
