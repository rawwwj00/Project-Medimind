import os
os.environ["TZ"] = "UTC"  # Force internal time to UTC

from flask import Flask, render_template, request
import firebase_admin
from firebase_admin import credentials, firestore, messaging
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
from google.oauth2 import service_account
from google.api_core import exceptions
import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
PROJECT_ID = "banded-oven-454521-k7"
FIRESTORE_LOCATION = "nam5"           # Delhi (Firestore)
CLOUD_TASKS_LOCATION = "asia-south1"   # Mumbai (Cloud Tasks)
QUEUE_NAME = "remainder-queue1"
DATABASE_NAME = "medimind"
SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"

# Timezone constants
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
IST_TIMEZONE = datetime.timezone(IST_OFFSET)
UTC_TIMEZONE = datetime.timezone.utc

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
    parent = task_client.queue_path(PROJECT_ID, CLOUD_TASKS_LOCATION, QUEUE_NAME)
    
    # Verify queue exists
    try:
        queue = task_client.get_queue(name=parent)
        logger.info(f"✅ Cloud Tasks queue ready in {CLOUD_TASKS_LOCATION}: {queue.name}")
    except exceptions.NotFound:
        logger.error("❌ Queue not found. Create it with:")
        logger.error(f"gcloud tasks queues create {QUEUE_NAME} --project={PROJECT_ID} --location={CLOUD_TASKS_LOCATION}")
        raise

except Exception as e:
    logger.error(f"🚨 Cloud Tasks initialization failed: {str(e)}")
    raise

app = Flask(__name__)
app.secret_key = "some-secret-key"  # Replace with your real secret key

@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")  # Your HTML file with the form

@app.route("/submit", methods=["POST"])
def submit():
    try:
        # Read JSON data from the request
        data = request.get_json()
        if not data:
            return "No JSON data received.", 400

        name = data.get("name", "").strip()
        medicine = data.get("medicine", "").strip()
        time_str = data.get("time", "").strip()
        fcm_token = data.get("fcm_token", "").strip()

        if not all([name, medicine, time_str, fcm_token]):
            return "All fields (including token) are required!", 400

        try:
            naive_time = datetime.datetime.fromisoformat(time_str)
            ist_time = naive_time.replace(tzinfo=IST_TIMEZONE)
            reminder_time = ist_time.astimezone(UTC_TIMEZONE)
        except ValueError as e:
            logger.error(f"Invalid time format: {str(e)}")
            return "Invalid time format! Use YYYY-MM-DDTHH:MM", 400

        current_utc = datetime.datetime.now(UTC_TIMEZONE)
        if reminder_time <= current_utc:
            return "Reminder time must be in the future!", 400

        # Save the reminder to Firestore, including the FCM token
        try:
            doc_ref = db.collection("reminders").document()
            doc_ref.set({
                "name": name,
                "medicine": medicine,
                "reminder_time": reminder_time,
                "status": "scheduled",
                "fcm_token": fcm_token,
                "created_at": firestore.SERVER_TIMESTAMP
            })
        except Exception as e:
            logger.error(f"Firestore save failed: {str(e)}")
            return "Database error. Please try again.", 500

        # Create a Cloud Task scheduled for the reminder time
        try:
            ts = timestamp_pb2.Timestamp()
            ts.FromDatetime(reminder_time)
            task = {
                "app_engine_http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "relative_uri": "/send-reminder",
                    "body": doc_ref.id.encode()  # Pass the document ID in the task body
                },
                "schedule_time": ts
            }
            created_task = task_client.create_task(parent=parent, task=task)
            logger.info(f"Created Cloud Task: {created_task.name}")
        except Exception as e:
            logger.error(f"Cloud Task creation failed: {str(e)}")
            doc_ref.delete()  # Roll back if task creation fails
            return "Scheduling error. Please try again.", 500

        display_time = reminder_time.astimezone(IST_TIMEZONE)
        return f"Reminder set for {name} at {display_time.strftime('%Y-%m-%d %H:%M IST')}!", 200

    except Exception as e:
        logger.error(f"Submission error: {str(e)}", exc_info=True)
        return "Server error. Please try again.", 500

@app.route("/send-reminder", methods=["POST"])
def send_reminder():
    try:
        # Retrieve the Firestore document ID from the task body
        doc_id = request.get_data(as_text=True)
        doc_ref = db.collection("reminders").document(doc_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return "Document not found", 404

        data = doc.to_dict()

        # ✅ Prevent duplicate notifications
        if data.get("status") == "completed":
            logger.info(f"Skipping duplicate notification for {doc_id}")
            return "Already processed", 200

        # Update status to processing
        doc_ref.update({"status": "processing"})

        # Get the FCM token stored with the reminder
        user_token = data.get("fcm_token")
        if not user_token:
            return "No FCM token found for this reminder.", 400

        try:
            # Send FCM notification
            message = messaging.Message(
                notification=messaging.Notification(
                    title="💊 Medicine Reminder",
                    body=f"Hi {data['name']}! Time to take {data['medicine']}"
                ),
                webpush=messaging.WebpushConfig(
                    notification=messaging.WebpushNotification(
                        icon="https://www.medimind.live/assets/images/Untitled%20design%20(6).png",
                        image="https://www.medimind.live/assets/images/khaiyena.png"
                    )
                ),
                token=user_token
            )
            messaging.send(message)

            # Mark reminder as completed
            doc_ref.update({"status": "completed"})
            return "Reminder sent", 200

        except Exception as e:
            logger.error(f"FCM send failed: {str(e)}")
            doc_ref.update({"status": "failed"})
            return f"Notification failed: {str(e)}", 500

    except Exception as e:
        logger.error(f"Reminder processing failed: {str(e)}")
        return f"Error: {str(e)}", 500

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080)),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true"
    )
