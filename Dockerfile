FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" jinja2 pymysql itsdangerous python-multipart
WORKDIR /srv
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
