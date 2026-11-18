FROM registry.access.redhat.com/ubi9/python-311
WORKDIR /opt/app-root/src
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# Worker and Maintenance use the same image with a different command
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--threads", "4", "--access-logfile", "-", "app:app"]
