FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Браузер Playwright тут навмисно не ставиться: розвідку (discover.py) робимо локально,
# а на сервері джерело ztoe працюватиме через прямий HTTP-запит до API.
CMD ["python", "bot.py"]
