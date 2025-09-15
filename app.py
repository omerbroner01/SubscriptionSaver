from datetime import datetime, timedelta
import os
from flask import Flask, request, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import stripe

# --- Env/config ---
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key')

# SQLite in instance folder
os.makedirs(app.instance_path, exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL',
    f"sqlite:///{os.path.join(app.instance_path, 'subscriptions.db')}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Login manager
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PRICE_ID = os.getenv('STRIPE_PRICE_ID')
PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY')

# Free plan limit
FREE_LIMIT = 5

# --- Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    premium = db.Column(db.Boolean, default=False)
    subscriptions = db.relationship('Subscription', backref='user', lazy=True)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False)
    renewal_date = db.Column(db.Date, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Helpers ---
def upcoming_badge(date_obj):
    # show "Due soon" if within 7 days
    return (date_obj - datetime.utcnow().date()).days <= 7

# --- Routes ---

# Landing OR dashboard
@app.route('/', methods=['GET', 'POST'])
def index():
    if not current_user.is_authenticated:
        # logged-out visitors see the public landing page
        return render_template('landing.html')

    # logged-in users see the dashboard (same behavior as before)
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        price_raw = (request.form.get('price') or '').strip()
        date_str = (request.form.get('date') or '').strip()

        # Free-plan guard
        if not current_user.premium and Subscription.query.filter_by(user_id=current_user.id).count() >= FREE_LIMIT:
            flash(f"Free limit reached ({FREE_LIMIT}). Upgrade for unlimited.", "warning")
            return redirect(url_for('index'))

        try:
            price = float(price_raw)
            date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            flash("Please fill in all fields with valid values.", "warning")
            return redirect(url_for('index'))

        sub = Subscription(name=name, price=price, renewal_date=date_val, user_id=current_user.id)
        db.session.add(sub)
        db.session.commit()
        flash(f"Added subscription: {name}", "success")
        return redirect(url_for('index'))

    # GET dashboard
    subs = Subscription.query.filter_by(user_id=current_user.id).all()
    total = sum(s.price for s in subs)
    upcoming_names = [s.name for s in subs if upcoming_badge(s.renewal_date)]
    return render_template(
        'index.html',
        subscriptions=subs,
        subs_total=total,
        upcoming_names=upcoming_names,
        is_premium=current_user.premium
    )

# Sign up
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = (request.form.get('password') or '').strip()
        if not email or not password:
            flash("Email and password are required.", "warning")
            return redirect(url_for('signup'))
        if User.query.filter_by(email=email).first():
            flash("Email already registered — try logging in.", "warning")
            return redirect(url_for('login'))
        user = User(email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Account created — you're now logged in", "success")
        return redirect(url_for('index'))
    return render_template('signup.html')

# Log in/out
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = (request.form.get('password') or '').strip()
        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password", "warning")
            return redirect(url_for('login'))
        login_user(user)
        flash("Logged in", "info")
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for('index'))

# Delete subscription
@app.route('/delete/<int:sub_id>', methods=['POST', 'GET'])
@login_required
def delete(sub_id):
    sub = Subscription.query.filter_by(id=sub_id, user_id=current_user.id).first_or_404()
    db.session.delete(sub)
    db.session.commit()
    flash("Subscription deleted", "info")
    return redirect(url_for('index'))

# Upgrade page
@app.route('/upgrade')
@login_required
def upgrade():
    if current_user.premium:
        flash("You're already Premium.", "success")
        return redirect(url_for('index'))
    return render_template('upgrade.html')

# Create Stripe Checkout session
@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    if not STRIPE_PRICE_ID:
        flash("Stripe price not configured.", "warning")
        return redirect(url_for('upgrade'))
    try:
        domain = request.host_url.rstrip('/')
        session = stripe.checkout.Session.create(
            mode='subscription',
            payment_method_types=['card'],
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{domain}/upgrade/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{domain}/upgrade",
            customer_email=current_user.email,
        )
        return redirect(session.url, code=303)
    except Exception as e:
        flash(f"Stripe error: {e}", "warning")
        return redirect(url_for('upgrade'))

# Handle success
@app.route('/upgrade/success')
@login_required
def upgrade_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash("Missing session id.", "warning")
        return redirect(url_for('upgrade'))
    try:
        s = stripe.checkout.Session.retrieve(session_id)
        if s.get('payment_status') == 'paid':
            current_user.premium = True
            db.session.commit()
            flash("Upgrade successful! Your account is now Premium.", "success")
        else:
            flash("Payment not completed yet.", "warning")
    except Exception as e:
        flash(f"Could not verify payment: {e}", "warning")
    return redirect(url_for('index'))

# Billing portal (manage subscription)
@app.route('/billing', methods=['GET'])
@login_required
def billing():
    try:
        # Create a customer on the fly if needed, using email as lookup
        customers = stripe.Customer.list(email=current_user.email).data
        if customers:
            customer_id = customers[0].id
        else:
            customer = stripe.Customer.create(email=current_user.email)
            customer_id = customer.id

        domain = request.host_url.rstrip('/')
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{domain}",
        )
        return redirect(portal.url, code=303)
    except Exception as e:
        flash(f"Could not open billing portal: {e}", "warning")
        return redirect(url_for('index'))

if __name__ == "__main__":
    app.run(debug=True)
