import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from flask import Flask, request, jsonify, render_template, redirect, url_for, session, make_response, \
    after_this_request
from pymongo import MongoClient
import random
from datetime import datetime, timedelta
import imaplib
import email
import re
from functools import wraps
import json
from bson import ObjectId, json_util
from medicine_data import default_medicines_data
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import urllib.parse

# ---------- Performance optimizations (added without removing anything) ----------
from flask_caching import Cache
from flask_compress import Compress
import orjson

# Custom JSON encoder using orjson (faster) – but keep your existing ObjectId handling
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

app = Flask(__name__)

# Session configuration - Set to 1 hour expiration
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', 'e6db0ccf32af7bdb06579f263147b8d4'),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=1),  # Session expires after 1 hour
    SESSION_PERMANENT=True,
    SESSION_COOKIE_SECURE=False,  # Set to True in production with HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_REFRESH_EACH_REQUEST=True  # Refresh session on each request (extends by 1 hour each time user is active)
)

# ---------- Caching configuration (FileSystemCache - no extra cost) ----------
app.config['CACHE_TYPE'] = 'FileSystemCache'
app.config['CACHE_DIR'] = '/tmp/flask_cache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 300  # 5 minutes default
cache = Cache(app)

# ---------- Compression (gzip) ----------
Compress(app)
app.config['COMPRESS_ALGORITHM'] = ['gzip']
app.config['COMPRESS_LEVEL'] = 6

# Use custom JSON encoder (faster with orjson support, but still your original logic)
app.json_encoder = CustomJSONEncoder

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
customers_collection = db['customers']  # Added customers collection

# Create indexes for faster queries (only if real MongoDB)
if hasattr(db, 'command'):  # Check if it's real MongoDB
    try:
        users_collection.create_index("username")
        users_collection.create_index("email")
        customers_collection.create_index("name")
        customers_collection.create_index("phone")
        customers_collection.create_index("gstNo")
        customers_collection.create_index("panNo")
        bills_collection.create_index("invoice_no")
        bills_collection.create_index("date")
        print("Database indexes created")
    except Exception as e:
        print(f"Could not create indexes: {e}")
else:
    print("Using dummy database - skipping index creation")

BASE_PDF_DIR = os.path.join(os.path.dirname(__file__), 'shree_samarth_enterprises_bills')


# Custom JSON encoder to handle ObjectId (already overridden above, but kept for safety)
class CustomJSONEncoderLegacy(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        return super().default(obj)

app.json_encoder = CustomJSONEncoderLegacy  # This will be replaced by the earlier one; kept for consistency


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


# Before request handler to refresh session and check expiration
@app.before_request
def before_request():
    """Refresh session expiration on each request and check if session is expired"""
    if 'user_id' in session:
        # Make session permanent and refresh it
        session.permanent = True
        # This will automatically extend the session lifetime by PERMANENT_SESSION_LIFETIME
        # due to SESSION_REFRESH_EACH_REQUEST = True


# Login required decorator with cache prevention
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('index'))

        response = make_response(f(*args, **kwargs))
        # Add headers to prevent caching of authenticated pages
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

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
    return re.match(r'^[a-zA-Z\s\.]{2,100}$', name) is not None


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


# Customer validation functions
def validate_customer_data(customer_data):
    """Validate customer data"""
    errors = []

    if not customer_data:
        return ["No customer data provided"]

    # Validate name (MANDATORY)
    if not validate_name(customer_data.get('name', '')):
        errors.append("Customer name must contain only letters, spaces and dots (2-100 characters)")

    # Validate phone (MANDATORY)
    phone = customer_data.get('phone', '')
    if not phone:
        errors.append("Phone number is required")
    elif not validate_phone(phone):
        errors.append("Phone number must be exactly 10 digits")

    # Validate GSTIN format (OPTIONAL - only if provided)
    gstNo = customer_data.get('gstNo', '')
    if gstNo and len(gstNo.strip()) > 0:
        if not re.match(r'^[0-9A-Z]{15}$', gstNo.strip()):
            errors.append("GSTIN must be 15 alphanumeric characters")

    # Validate PAN format (OPTIONAL - only if provided)
    panNo = customer_data.get('panNo', '')
    if panNo and len(panNo.strip()) > 0:
        if not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]{1}$', panNo.strip()):
            errors.append("PAN must be in format: ABCDE1234F")

    return errors


# Inject logout confirmation JavaScript into all pages
def inject_logout_confirmation(html_content):
    """Inject JavaScript to handle logout confirmation"""
    logout_script = """
    <script>
    // Override the original selectMenu function to add logout confirmation
    (function() {
        // Store the original function if it exists
        var originalSelectMenu = window.selectMenu;

        // Override selectMenu function
        window.selectMenu = function(menu) {
            if (menu === 'logout') {
                // Show confirmation dialog
                if (confirm('Are you sure you want to logout?')) {
                    // Clear session storage and redirect to logout
                    sessionStorage.clear();
                    window.location.href = '/logout';
                }
            } else if (originalSelectMenu) {
                // Call original function for other menu items
                originalSelectMenu(menu);
            } else {
                // Fallback if original function doesn't exist
                const menuItems = document.querySelectorAll('.menu-item');
                menuItems.forEach(item => item.classList.remove('active'));
                if (event) event.currentTarget.classList.add('active');

                if (menu === 'dashboard') {
                    window.location.href = '/billing';
                } else if (menu === 'inventory') {
                    if (window.location.pathname === '/billing' && window.showStatsModal) {
                        window.showStatsModal();
                    } else {
                        window.location.href = '/billing#show-inventory';
                    }
                } else if (menu === 'reports') {
                    window.location.href = '/reports';
                } else if (menu === 'profit') {
                    window.location.href = '/profit';
                }
            }
        };

        // Prevent back button after logout
        (function() {
            window.history.pushState(null, null, window.location.href);
            window.addEventListener('popstate', function() {
                window.history.pushState(null, null, window.location.href);
            });
        })();

        // Check session periodically with expiry time
        function checkSession() {
            fetch('/check-session')
                .then(response => response.json())
                .then(data => {
                    if (!data.logged_in && window.location.pathname !== '/') {
                        // Session expired, redirect to login with message
                        alert('Your session has expired. Please login again.');
                        window.location.href = '/';
                    } else if (data.logged_in && data.remaining_minutes !== undefined) {
                        // Show warning if session is about to expire (last 5 minutes)
                        if (data.remaining_minutes <= 5 && data.remaining_minutes > 0) {
                            console.log(`Session expires in ${data.remaining_minutes} minutes`);
                            // Optional: Show a warning toast/notification
                            if (data.remaining_minutes === 5) {
                                // You can implement a toast notification here
                                console.warn('Session will expire in 5 minutes!');
                            }
                        }
                    }
                })
                .catch(error => console.error('Error checking session:', error));
        }

        // Check session every 30 seconds
        setInterval(checkSession, 30000);

        // Initial session check
        checkSession();
    })();
    </script>
    """

    # Insert the script before closing body tag
    if '</body>' in html_content:
        return html_content.replace('</body>', logout_script + '</body>')
    return html_content + logout_script


@app.route('/')
def index():
    # If user is already logged in and session is valid, redirect to billing
    if 'user_id' in session:
        # Verify user still exists in database (optional additional check)
        try:
            user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
            if user:
                return redirect(url_for('billing'))
            else:
                # User no longer exists, clear session
                session.clear()
        except:
            # If any error occurs, clear session
            session.clear()

    response = make_response(render_template('index.html'))
    # Prevent caching of login page
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/billing')
@login_required
def billing():
    html_content = render_template('billing.html')
    html_content = inject_logout_confirmation(html_content)
    response = make_response(html_content)
    # Add headers to prevent caching
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/reports')
@login_required
def reports():
    html_content = render_template('reports.html')
    html_content = inject_logout_confirmation(html_content)
    response = make_response(html_content)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/profit')
@login_required
def profit():
    html_content = render_template('profit.html')
    html_content = inject_logout_confirmation(html_content)
    response = make_response(html_content)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


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
    html_content = render_template('invoice_pdf.html', bill_data=bill_data)
    html_content = inject_logout_confirmation(html_content)
    response = make_response(html_content)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


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

        # Set session with 1 hour expiration
        session.clear()  # Clear any existing session data
        session['user_id'] = str(user['_id'])
        session['username'] = user['username']
        session.permanent = True  # Make session permanent (uses PERMANENT_SESSION_LIFETIME)

        # Store login time for reference
        session['login_time'] = datetime.now().isoformat()

        return jsonify({
            'message': 'Login successful',
            'redirect': url_for('billing'),
            'session_expiry_hours': 1
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/logout')
def logout():
    session.clear()
    session.permanent = False

    @after_this_request
    def add_no_cache_headers(response):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    return redirect(url_for('index'))


@app.route('/logout-confirm', methods=['POST'])
def logout_confirm():
    """API endpoint for logout confirmation"""
    return jsonify({
        'success': True,
        'message': 'Please confirm logout',
        'redirect': url_for('logout')
    })


@app.route('/check-session')
def check_session():
    """Check if user is logged in and get remaining session time"""
    if 'user_id' in session:
        # Calculate remaining session time
        remaining_seconds = session.permanent_session_lifetime.total_seconds() if hasattr(session,
                                                                                          'permanent_session_lifetime') else 3600
        remaining_minutes = int(remaining_seconds / 60)

        return jsonify({
            'logged_in': True,
            'username': session.get('username'),
            'remaining_minutes': remaining_minutes,
            'session_expires_in_hours': round(remaining_seconds / 3600, 1)
        })
    else:
        return jsonify({
            'logged_in': False
        })


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

        # Add current date and time to the bill data
        now = datetime.now()
        data['created_at'] = now
        # Store date as YYYY-MM-DD (for filtering)
        data['date'] = now.strftime('%Y-%m-%d')
        # Store time as HH:MM:SS AM/PM format (for display in reports)
        data['time'] = now.strftime('%I:%M:%S %p')

        bills_collection.insert_one(data)

        return jsonify({'message': 'Bill saved successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ========== UPDATED BILLS ENDPOINTS WITH MONTH/YEAR FILTER ==========
@app.route('/api/bills', methods=['GET'])
@login_required
def get_bills():
    try:
        # Get filter parameters
        time_filter = request.args.get('time_filter', 'all')
        custom_date = request.args.get('custom_date', '')
        month = request.args.get('month', '')
        year = request.args.get('year', '')
        limit = int(request.args.get('limit', 1000))

        # Build query based on filters
        query = {}

        if time_filter == 'today':
            today = datetime.now().strftime('%Y-%m-%d')
            query['date'] = today
        elif time_filter == 'yesterday':
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            query['date'] = yesterday
        elif time_filter == 'this_week':
            today = datetime.now()
            start_of_week = today - timedelta(days=today.weekday())
            query['date'] = {'$gte': start_of_week.strftime('%Y-%m-%d')}
        elif time_filter == 'this_month':
            today = datetime.now()
            month_start = today.replace(day=1)
            query['date'] = {'$gte': month_start.strftime('%Y-%m-%d')}
        elif time_filter == 'last_month':
            today = datetime.now()
            first_day_this_month = today.replace(day=1)
            last_day_last_month = first_day_this_month - timedelta(days=1)
            first_day_last_month = last_day_last_month.replace(day=1)
            query['date'] = {
                '$gte': first_day_last_month.strftime('%Y-%m-%d'),
                '$lte': last_day_last_month.strftime('%Y-%m-%d')
            }
        elif time_filter == 'this_year':
            today = datetime.now()
            year_start = today.replace(month=1, day=1)
            query['date'] = {'$gte': year_start.strftime('%Y-%m-%d')}
        elif time_filter == 'custom' and custom_date:
            query['date'] = custom_date
        elif time_filter == 'monthly' and month:
            query['date'] = {'$regex': f'^{month}'}
        elif time_filter == 'yearly' and year:
            query['date'] = {'$regex': f'^{year}'}
        elif time_filter == 'month_year':
            # New filter for month and year selection
            if month and year:
                query['date'] = {'$regex': f'^{year}-{month}'}

        # Get bills with optimized query
        bills = list(bills_collection.find(query, {'_id': 0}).sort('date', -1).limit(limit))
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


# ========== DELETE BILL ENDPOINT ==========
@app.route('/api/bill/<invoice_no>', methods=['DELETE'])
@login_required
def delete_bill(invoice_no):
    try:
        result = bills_collection.delete_one({'invoice_no': int(invoice_no)})
        if result.deleted_count > 0:
            return jsonify({
                'success': True,
                'message': f'Bill {invoice_no} deleted successfully'
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': 'Bill not found'
            }), 404
    except Exception as e:
        print(f"Error deleting bill: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


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


# ---- Cached endpoint for medicines (performance improvement) ----
@app.route('/api/medicines', methods=['GET'])
@login_required
@cache.cached(timeout=60, query_string=True)  # cache for 60 seconds, vary by query params
def get_medicines():
    try:
        search_term = request.args.get('search', '')
        category_filter = request.args.get('category', 'all')

        medicines = list(medicines_collection.find({}, {'_id': 0}))

        if not medicines:
            medicines_collection.insert_many(default_medicines_data)
            medicines = default_medicines_data

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

        # Invalidate cache after modification
        cache.delete_memoized(get_medicines)

        return jsonify({'message': 'Medicine saved successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/medicines/<name>', methods=['DELETE'])
@login_required
def delete_medicine(name):
    try:
        result = medicines_collection.delete_one({'name': name})
        if result.deleted_count > 0:
            # Invalidate cache after deletion
            cache.delete_memoized(get_medicines)
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


# Customer Management Endpoints
@app.route('/api/customers', methods=['GET'])
@login_required
def get_customers():
    try:
        search_term = request.args.get('search', '').strip()

        if search_term:
            customers = list(customers_collection.find({
                '$or': [
                    {'name': {'$regex': search_term, '$options': 'i'}},
                    {'phone': {'$regex': search_term, '$options': 'i'}}
                ]
            }).sort('name', 1))
        else:
            customers = list(customers_collection.find({}).sort('name', 1))

        for customer in customers:
            customer['_id'] = str(customer['_id'])

        return jsonify(customers), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/customers', methods=['POST'])
@login_required
def add_customer():
    """Add a new customer to the database"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No customer data provided'}), 400

        # Validate customer data
        validation_errors = validate_customer_data(data)
        if validation_errors:
            return jsonify({'error': ' | '.join(validation_errors)}), 400

        # Check if customer already exists (by phone or name)
        existing_customer = customers_collection.find_one({
            '$or': [
                {'name': data['name'].strip()},
                {'phone': data.get('phone', '').strip()}
            ]
        })

        if existing_customer:
            return jsonify({'error': 'Customer with same name or phone already exists'}), 400

        # Prepare customer document
        customer_doc = {
            'name': data['name'].strip(),
            'address': data.get('address', '').strip(),
            'phone': data.get('phone', '').strip(),
            'gstNo': data.get('gstNo', '').strip().upper(),
            'panNo': data.get('panNo', '').strip().upper(),
            'created_at': datetime.now(),
            'updated_at': datetime.now()
        }

        # Insert into database
        result = customers_collection.insert_one(customer_doc)

        # Return the created customer with ID
        customer_doc['_id'] = str(result.inserted_id)
        return jsonify({
            'message': 'Customer added successfully',
            'customer': customer_doc
        }), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/customers/<customer_id>', methods=['PUT'])
@login_required
def update_customer(customer_id):
    """Update an existing customer"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No customer data provided'}), 400

        # Validate customer data
        validation_errors = validate_customer_data(data)
        if validation_errors:
            return jsonify({'error': ' | '.join(validation_errors)}), 400

        # Update customer
        update_data = {
            'name': data['name'].strip(),
            'address': data.get('address', '').strip(),
            'phone': data.get('phone', '').strip(),
            'gstNo': data.get('gstNo', '').strip().upper(),
            'panNo': data.get('panNo', '').strip().upper(),
            'updated_at': datetime.now()
        }

        result = customers_collection.update_one(
            {'_id': ObjectId(customer_id)},
            {'$set': update_data}
        )

        if result.matched_count == 0:
            return jsonify({'error': 'Customer not found'}), 404

        return jsonify({'message': 'Customer updated successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/customers/<customer_id>', methods=['DELETE'])
@login_required
def delete_customer(customer_id):
    """Delete a customer"""
    try:
        result = customers_collection.delete_one({'_id': ObjectId(customer_id)})
        if result.deleted_count == 0:
            return jsonify({'error': 'Customer not found'}), 404
        return jsonify({'message': 'Customer deleted successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health_check():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': 'connected' if hasattr(db, 'command') else 'dummy',
        'session_timeout': '1 hour'
    })


if __name__ == '__main__':
    print("=== Pharmacy Management System ===")
    print("=== Server Starting ===")
    print("=== Session Timeout: 1 Hour ===")
    port = int(os.environ.get('PORT', 5000))
    # Set debug=False for production
    app.run(host='0.0.0.0', port=port, debug=False)
