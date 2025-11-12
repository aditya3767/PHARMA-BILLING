from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from pymongo import MongoClient
import random
from datetime import datetime, timedelta
import imaplib
import email
import re
from functools import wraps
import os
import json
from bson import ObjectId, json_util
from medicine_data import default_medicines_data
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import urllib.parse

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'e6db0ccf32af7bdb06579f263147b8d4')

# MongoDB connection with enhanced SSL handling
try:
    # Use environment variables for MongoDB
    username = os.environ.get('MONGODB_USERNAME', 'adityabhoir983_db_user')
    password = os.environ.get('MONGODB_PASSWORD', 'HiV2rwczhpH0Cpjq')
    encoded_password = urllib.parse.quote_plus(password)

    connection_string = f"mongodb+srv://{username}:{encoded_password}@cluster0.aavnxbi.mongodb.net/pharmacy_db?retryWrites=true&w=majority&appName=Cluster0"

    client = MongoClient(
        connection_string,
        tls=True,
        tlsAllowInvalidCertificates=False,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=10000,
        retryWrites=True
    )

    # Test the connection
    client.admin.command('ping')
    db = client['pharmacy_db']
    print("Connected to MongoDB successfully!")

except Exception as e:
    print(f"Could not connect to MongoDB: {e}")
    print("Using dummy database as fallback...")

    # Fallback - create a dummy client to prevent crashes
    class DummyDB:
        def __getitem__(self, name):
            return DummyCollection()

        def __getattr__(self, name):
            return DummyCollection()

    class DummyCollection:
        def __init__(self):
            self.data = []

        def find(self, *args, **kwargs):
            return self.data

        def find_one(self, *args, **kwargs):
            return None

        def insert_one(self, document, *args, **kwargs):
            if '_id' not in document:
                document['_id'] = ObjectId()
            self.data.append(document)
            return DummyResult(inserted_id=document.get('_id'))

        def update_one(self, filter, update, *args, **kwargs):
            return DummyResult(modified_count=0)

        def delete_one(self, filter, *args, **kwargs):
            return DummyResult(deleted_count=0)

        def replace_one(self, filter, replacement, *args, **kwargs):
            return DummyResult(modified_count=0)

        def insert_many(self, documents, *args, **kwargs):
            for doc in documents:
                if '_id' not in doc:
                    doc['_id'] = ObjectId()
                self.data.append(doc)
            return DummyResult(inserted_ids=[doc.get('_id') for doc in documents])

        def create_index(self, *args, **kwargs):
            return None

        def sort(self, *args, **kwargs):
            return self.data

    class DummyResult:
        def __init__(self, inserted_id=None, modified_count=0, deleted_count=0, inserted_ids=None):
            self.inserted_id = inserted_id
            self.modified_count = modified_count
            self.deleted_count = deleted_count
            self.inserted_ids = inserted_ids or []

    db = DummyDB()

users_collection = db['users']
medicines_collection = db['medicines']
bills_collection = db['bills']

# Create indexes for faster queries (only if real MongoDB)
if hasattr(db, 'command'):  # Check if it's real MongoDB
    try:
        users_collection.create_index("username")
        users_collection.create_index("email")
        print("Database indexes created")
    except Exception as e:
        print(f"Could not create indexes: {e}")
else:
    print("Using dummy database - skipping index creation")

BASE_PDF_DIR = os.path.join(os.path.dirname(__file__), 'shree_samarth_enterprises_bills')

# Custom JSON encoder to handle ObjectId
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        return super().default(obj)

app.json_encoder = CustomJSONEncoder

# Function to sync default medicines with database
def sync_default_medicines():
    try:
        # Get all existing medicines from database
        existing_medicines = list(medicines_collection.find({}, {'_id': 0, 'name': 1}))
        existing_names = [med['name'] for med in existing_medicines]

        # Update or insert default medicines
        for medicine in default_medicines_data:
            if medicine['name'] in existing_names:
                # Update existing medicine
                medicines_collection.update_one(
                    {'name': medicine['name']},
                    {'$set': {
                        'category': medicine['category'],
                        'variants': medicine['variants']
                    }}
                )
            else:
                # Insert new medicine
                medicines_collection.insert_one(medicine)

        print("Default medicines synced with database")
    except Exception as e:
        print(f"Error syncing default medicines: {e}")

# Call this function when the app starts
sync_default_medicines()

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# Validation functions
def validate_email(email):
    """Validate email format"""
    if not email or not isinstance(email, str):
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email.strip()) is not None

def validate_phone(phone):
    """Validate phone number (10 digits)"""
    if not phone or not isinstance(phone, str):
        return False
    # Remove any non-digit characters and check length
    cleaned_phone = re.sub(r'\D', '', phone)
    return len(cleaned_phone) == 10

def validate_name(name):
    """Validate name (letters and spaces only, 2-50 characters)"""
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    return re.match(r'^[a-zA-Z\s]{2,50}$', name) is not None

def validate_age(age):
    """Validate age (15-80)"""
    try:
        if not age:
            return False
        age_int = int(age)
        return 15 <= age_int <= 80
    except (ValueError, TypeError):
        return False

def validate_password(password):
    """Validate password strength"""
    if not password or not isinstance(password, str):
        return False, "Password is required"
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r'\d', password):
        return False, "Password must contain at least one number"
    if not re.search(r'[@$!%*?&]', password):
        return False, "Password must contain at least one special character (@$!%*?&)"
    return True, "Password is strong"

def validate_user_data(user_data):
    """Comprehensive user data validation"""
    errors = []

    if not user_data:
        return ["No user data provided"]

    # Validate full name
    if not validate_name(user_data.get('fullName')):
        errors.append("Full name must contain only letters and spaces (2-50 characters)")

    # Validate age
    if not validate_age(user_data.get('age')):
        errors.append("Age must be a number between 15 and 80")

    # Validate gender
    gender = user_data.get('gender')
    if not gender or gender not in ['male', 'female', 'other']:
        errors.append("Please select a valid gender")

    # Validate email
    if not validate_email(user_data.get('email')):
        errors.append("Please enter a valid email address")

    # Validate phone
    if not validate_phone(user_data.get('phone')):
        errors.append("Phone number must be exactly 10 digits")

    # Validate password
    is_valid_password, password_error = validate_password(user_data.get('password'))
    if not is_valid_password:
        errors.append(password_error)

    return errors

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/billing')
@login_required
def billing():
    return render_template('billing.html')

@app.route('/reports')
@login_required
def reports():
    return render_template('reports.html')

@app.route('/profit')
@login_required
def profit():
    return render_template('profit.html')

@app.route('/report/<invoice_no>')
@login_required
def report(invoice_no):
    bill = bills_collection.find_one({'invoice_no': invoice_no})
    if bill:
        return render_template('report_details.html', bill=bill)
    return 'Bill not found', 404

@app.route('/invoice_pdf')
@login_required
def invoice_pdf():
    # Get bill data from session or request args
    bill_data = session.get('bill_data', {})
    return render_template('invoice_pdf.html', bill_data=bill_data)

@app.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json()
        user_data = data.get('user_data')

        if not user_data:
            return jsonify({'error': 'No user data provided'}), 400

        # Comprehensive user data validation
        validation_errors = validate_user_data(user_data)
        if validation_errors:
            return jsonify({'error': ' | '.join(validation_errors)}), 400

        # Generate username from email
        username = user_data['email'].split('@')[0]

        # Check if user already exists - Enhanced check with better error message
        existing_user = users_collection.find_one({'email': user_data['email'].strip().lower()})
        if existing_user:
            return jsonify({'error': 'This email is already registered. Please use a different email address or login with your existing account.'}), 400

        # Also check if username already exists
        existing_username = users_collection.find_one({'username': username})
        if existing_username:
            return jsonify({'error': 'Username already exists. Please try a different email address.'}), 400

        # Data sanitization before storing
        sanitized_user_data = {
            'fullName': user_data['fullName'].strip(),
            'age': int(user_data['age']),
            'gender': user_data['gender'],
            'phone': re.sub(r'\D', '', user_data['phone']),  # Clean phone number
            'password': user_data['password'],  # Store plaintext password
            'username': username,
            'email': user_data['email'].strip().lower(),
            'verified': True,
            'registration_date': datetime.now().isoformat()
        }

        # Store user in MongoDB
        user_doc = {
            'fullName': sanitized_user_data['fullName'],
            'age': sanitized_user_data['age'],
            'gender': sanitized_user_data['gender'],
            'phone': sanitized_user_data['phone'],
            'password': sanitized_user_data['password'],
            'username': sanitized_user_data['username'],
            'email': sanitized_user_data['email'],
            'verified': True,
            'registration_date': datetime.now().isoformat()
        }
        users_collection.insert_one(user_doc)

        return jsonify({'message': 'Registration successful! You can now login.'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        username_or_email = data.get('username_or_email')
        password = data.get('password')

        if not username_or_email or not password:
            return jsonify({'error': 'Missing username/email or password'}), 400

        # Basic input validation
        username_or_email = username_or_email.strip()
        if len(username_or_email) < 2:
            return jsonify({'error': 'Invalid username or email'}), 400

        if len(password) < 1:
            return jsonify({'error': 'Password cannot be empty'}), 400

        # Find user by username or email
        user = users_collection.find_one({
            '$or': [
                {'username': username_or_email},
                {'email': username_or_email}
            ]
        })

        if not user:
            return jsonify({'error': 'Invalid credentials'}), 401

        # Check password (direct comparison since it's plaintext)
        if user['password'] != password:
            return jsonify({'error': 'Invalid credentials'}), 401

        # Set session
        session['user_id'] = str(user['_id'])
        session['username'] = user['username']

        return jsonify({'message': 'Login successful', 'redirect': url_for('billing')}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/save-bill-data', methods=['POST'])
@login_required
def save_bill_data():
    try:
        data = request.get_json()
        session['bill_data'] = data
        return jsonify({'message': 'Bill data saved successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/save_bill', methods=['POST'])
@login_required
def save_bill():
    try:
        data = request.get_json()
        # Ensure invoice_no is unique (simple check, in production use better method)
        existing = bills_collection.find_one({'invoice_no': data['invoice_no']})
        if existing:
            return jsonify({'error': 'Invoice number already exists'}), 400

        # Add timestamp to bill data
        data['created_at'] = datetime.now()
        bills_collection.insert_one(data)

        return jsonify({'message': 'Bill saved successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/bills', methods=['GET'])
@login_required
def get_bills():
    try:
        bills = list(bills_collection.find({}, {'_id': 0}).sort('date', -1))
        return jsonify(bills), 200
    except Exception as e:
        print(f"Error fetching bills: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/bill/<invoice_no>', methods=['GET'])
@login_required
def get_bill(invoice_no):
    try:
        bill = bills_collection.find_one({'invoice_no': int(invoice_no)}, {'_id': 0})
        if bill:
            return jsonify(bill), 200
        return jsonify({'error': 'Bill not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/save_invoice_pdf', methods=['POST'])
@login_required
def save_invoice_pdf():
    try:
        folder_path = request.form.get('folderPath')
        pdf_file = request.files.get('pdf')

        if not folder_path or not pdf_file:
            return jsonify({'error': 'Missing folder path or PDF file'}), 400

        # Sanitize folder_path to prevent including 'shree_samarth_enterprises_bills'
        folder_path = folder_path.replace('shree_samarth_enterprises_bills/', '').strip('/')

        # Construct the full path for saving the PDF
        today_folder = os.path.join(BASE_PDF_DIR, folder_path)
        os.makedirs(today_folder, exist_ok=True)

        pdf_path = os.path.join(today_folder, pdf_file.filename)
        pdf_file.save(pdf_path)

        return jsonify({
            'status': 'success',
            'message': f'PDF saved to {pdf_path}',
            'path': pdf_path
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to save PDF: {str(e)}'}), 500

# Medicine data endpoints
@app.route('/api/medicines', methods=['GET'])
@login_required
def get_medicines():
    try:
        # Get search and filter parameters
        search_term = request.args.get('search', '')
        category_filter = request.args.get('category', 'all')

        # Get medicines from MongoDB
        medicines = list(medicines_collection.find({}, {'_id': 0}))

        # If no medicines in DB, initialize with default data
        if not medicines:
            medicines_collection.insert_many(default_medicines_data)
            medicines = default_medicines_data

        # Apply filters
        filtered_medicines = medicines
        if search_term:
            filtered_medicines = [m for m in filtered_medicines if search_term.lower() in m['name'].lower()]
        if category_filter != 'all':
            filtered_medicines = [m for m in filtered_medicines if m['category'] == category_filter]

        return jsonify(filtered_medicines), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/medicines', methods=['POST'])
@login_required
def save_medicine():
    try:
        data = request.get_json()

        # Check if medicine already exists
        existing_medicine = medicines_collection.find_one({'name': data['name']})
        if existing_medicine:
            # Update existing medicine
            medicines_collection.update_one(
                {'name': data['name']},
                {'$set': {'variants': data['variants'], 'category': data['category']}}
            )
        else:
            # Insert new medicine
            medicines_collection.insert_one(data)

        return jsonify({'message': 'Medicine saved successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/medicines/<name>', methods=['DELETE'])
@login_required
def delete_medicine(name):
    try:
        result = medicines_collection.delete_one({'name': name})
        if result.deleted_count > 0:
            return jsonify({'message': 'Medicine deleted successfully'}), 200
        else:
            return jsonify({'error': 'Medicine not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications', methods=['GET'])
@login_required
def get_notifications():
    try:
        # Get all medicines
        medicines = list(medicines_collection.find({}, {'_id': 0}))
        notifications = []

        # Check for expiring medicines
        today = datetime.now()
        for medicine in medicines:
            for variant in medicine['variants']:
                if variant.get('expiry'):
                    try:
                        expiry_date = datetime.strptime(variant['expiry'], '%Y-%m-%d')
                        days_until_expiry = (expiry_date - today).days

                        if days_until_expiry < 0:
                            message = f"🚨 {medicine['name']} ({variant['size']}) has EXPIRED!"
                            priority = 'critical'
                        elif days_until_expiry <= 6:
                            message = f"⚠️ {medicine['name']} ({variant['size']}) expires in {days_until_expiry} days!"
                            priority = 'critical'
                        elif days_until_expiry <= 30:
                            message = f"⚠️ {medicine['name']} ({variant['size']}) expires in {days_until_expiry} days"
                            priority = 'high'
                        elif days_until_expiry <= 60:
                            message = f"ℹ️ {medicine['name']} ({variant['size']}) expires in {days_until_expiry} days"
                            priority = 'medium'
                        elif days_until_expiry <= 90:
                            message = f"ℹ️ {medicine['name']} ({variant['size']}) expires in {days_until_expiry} days"
                            priority = 'low'
                        else:
                            continue

                        notifications.append({
                            'message': message,
                            'priority': priority,
                            'date': today.strftime('%Y-%m-%d'),
                            'medicine': medicine['name'],
                            'variant': variant['size'],
                            'expiry': variant['expiry']
                        })
                    except ValueError:
                        # Skip if expiry date format is invalid
                        continue

        # Sort by priority (critical first)
        priority_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        notifications.sort(key=lambda x: priority_order[x['priority']])

        return jsonify(notifications), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health_check():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': 'connected' if hasattr(db, 'command') else 'dummy'
    })

if __name__ == '__main__':
    print("=== Pharmacy Management System ===")
    print("=== Server Starting ===")
    app.run(debug=True, host='0.0.0.0', port=5000)

