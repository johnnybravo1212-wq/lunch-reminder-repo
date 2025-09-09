# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# --- This is the crucial step that was likely missing ---
# Copy the rest of the application's code into the container
# This includes main.py, Procfile, and the entire templates directory
COPY . .

# Expose the port the app runs on
EXPOSE 8080

# Define the command to run the app using gunicorn
# This replaces the need for the Procfile
CMD ["gunicorn", "--workers", "4", "--timeout", "120", "--bind", "0.0.0.0:8080", "main:app"]
