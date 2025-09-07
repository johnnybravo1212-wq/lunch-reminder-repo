# Use the official lightweight Python image.
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the main application file
COPY main.py .

# Set the command to run the application using Gunicorn, a production-grade server
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]