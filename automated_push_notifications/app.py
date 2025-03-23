from flask import Flask, render_template, request, redirect, url_for, flash
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
from google.oauth2 import service_account
from google.api_core import exceptions
import datetime
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
PROJECT_ID = "banded-oven-454521-k7"
FIRESTORE_LOCATION = "nam5"
LOCATION = "asia-south1"  # Mumbai region
QUEUE_NAME = "remainder-queue1"
DATABASE_NAME = "medimind"
SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"

# Initialize Firebase
try:
    firebase_cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(firebase_cred)
    db = firestore.client(database_id=DATABASE_NAME)
    logger.info(f"✅ Firestore connected to '{DATABASE_NAME}' in {FIRESTORE_LOCATION}")
except Exception as e:
    logger.error(f"🔥 Firestore initialization failed: {str(e)}")
    raise

# Initialize Cloud Tasks
try:
    tasks_creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    task_client = tasks_v2.CloudTasksClient(credentials=tasks_creds)
    parent = task_client.queue_path(PROJECT_ID, LOCATION, QUEUE_NAME)

    # Verify queue exists
    try:
        queue = task_client.get_queue(name=parent)
        logger.info(f"✅ Cloud Tasks queue ready: {queue.name}")
    except exceptions.NotFound:
        logger.error(f"❌ Queue not found. Create it using:")
        logger.error(f"gcloud tasks queues create {QUEUE_NAME} \\")
        logger.error(f"  --project={PROJECT_ID} --location={LOCATION}")
        raise

except Exception as e:
    logger.error(f"🚨 Cloud Tasks initialization failed: {str(e)}")
    raise

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key-123")

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/save-token", methods=["POST"])
def save_token():
    """Save FCM token from client to Firestore."""
    try:
        token = request.json.get("token")
        user_id = "user123"  # Replace with actual user identification
        
        # Save to Firestore
        db.collection("users").document(user_id).update({
            "fcm_tokens": firestore.ArrayUnion([token])
        })
        
        return "Token saved", 200
    except Exception as e:
        logger.error(f"Token save error: {str(e)}")
        return "Error saving token", 500

@app.route("/submit", methods=["POST"])
def submit():
    """Handle form submission and schedule reminder."""
    try:
        # Get form data
        name = request.form.get("name", "").strip()
        medicine = request.form.get("medicine", "").strip()
        time_str = request.form.get("time", "").strip()

        # Validate inputs
        if not all([name, medicine, time_str]):
            flash("All fields are required!")
            return redirect(url_for("home"))

        # Parse time
        try:
            naive_time = datetime.datetime.fromisoformat(time_str)
            reminder_time = naive_time.astimezone(datetime.timezone.utc)
        except ValueError:
            flash("Invalid time format! Use YYYY-MM-DDTHH:MM")
            return redirect(url_for("home"))

        # Time validation
        if reminder_time < datetime.datetime.now(datetime.timezone.utc):
            flash("Reminder time must be in the future!")
            return redirect(url_for("home"))

        # Save to Firestore
        doc_ref = db.collection("reminders").document()
        doc_ref.set({
            "name": name,
            "medicine": medicine,
            "reminder_time": reminder_time,
            "status": "scheduled",
            "created_at": firestore.SERVER_TIMESTAMP,
            "user_id": "user123"  # Replace with actual user ID
        })

        # Create Cloud Task
        task = {
            "app_engine_http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "relative_uri": "/send-reminder",
                "body": doc_ref.id.encode()
            },
            "schedule_time": timestamp_pb2.Timestamp().FromDatetime(reminder_time)
        }

        created_task = task_client.create_task(parent=parent, task=task)
        logger.info(f"Created task: {created_task.name}")
        flash(f"✅ Reminder set for {name} at {reminder_time.strftime('%Y-%m-%d %H:%M UTC')}!")
        return redirect(url_for("home"))

    except Exception as e:
        logger.error(f"Submission error: {str(e)}", exc_info=True)
        flash("Server error. Please try again.")
        return redirect(url_for("home"))

@app.route("/send-reminder", methods=["POST"])
def send_reminder():
    try:
        doc_id = request.get_data(as_text=True)
        logger.info(f"Processing doc: {doc_id}")

        # Verify document exists
        doc_ref = db.collection("reminders").document(doc_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.error(f"Document {doc_id} does not exist")
            return "Document not found", 404

        # Verify user data
        data = doc.to_dict()
        user_ref = db.collection("users").document(data["user_id"])
        user_data = user_ref.get().to_dict()
        if not user_data or "fcm_tokens" not in user_data:
            logger.error(f"User {data['user_id']} has no FCM tokens")
            return "Invalid user configuration", 400

        # Validate token
        token = user_data["fcm_tokens"][0]
        if not token or len(token) < 50:  # Basic token format check
            logger.error(f"Invalid token format: {token}")
            return "Invalid FCM token", 400

        # Send notification
        message = messaging.Message(...)
        response = messaging.send(message)
        logger.info(f"Notification sent: {response}")
        return "Success", 200

    except Exception as e:
        logger.error(f"Critical failure: {str(e)}", exc_info=True)
        return "Internal error", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)