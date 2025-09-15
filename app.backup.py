import os
from datetime import datetime, timedelta

from flask import Flask, request, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
import stripe

# --- Load .env if present ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key')

# --- Database: store under the "instance" folder ---
os.makedirs(app.instance_path, exist_ok=True)
default_sqlite_path = 'sqlite:///' + os.path.join(app.instance_path, 'subscriptions.db')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', default_sqlite_path)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Login manager ---
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- Stripe config (test mode keys live in .env) ---
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PRICE_ID = os.getenv('STRIPE_PRICE_ID')

# --- Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_premium = db.Column(db.Boolean, default=False)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False)
    renewal_date = db.Column(db.Date, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('subscriptions', lazy=True))

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

FREE_LIMIT = 5  # free-tier limit

# --- Auth routes ---
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = (request.form.get('password') or '').strip()
        if not email or not password:
            flash("Email and password are required", "warning")
            return redirect(url_for('signup'))
        if User.query.filter_by(email=email).first():
            flash("Email already registered — try logging in", "warning")
            return redirect(url_for('login'))
        user = User(email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Account created — you're now logged in", "success")
        return redirect(url_for('index'))
    return render_template('signup.html')

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
        flash("Logged in", "success")
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for('login'))

# --- Dashboard ---
@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        price = (request.form.get('price') or '').strip()
        date_str = (request.form.get('renewal_date') or '').strip()

        if not (name and price and date_str):
            flash("Please fill in all fields", "warning")
            return redirect(url_for('index'))

        try:
            price_val = float(price)
        except ValueError:
            flash("Price must be a number", "warning")
            return redirect(url_for('index'))

        try:
            rdate = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Date must be in YYYY-MM-DD format", "warning")
            return redirect(url_for('index'))

        # enforce free-tier limit for non-premium users
        current_count = Subscription.query.filter_by(user_id=current_user.id).count()
        if not current_user.is_premium and current_count >= FREE_LIMIT:
            flash("Free limit reached (5). Upgrade to Premium for unlimited subscriptions.", "warning")
            return redirect(url_for('index'))

        db.session.add(Subscription(name=name, price=price_val, renewal_date=rdate, user_id=current_user.id))
        db.session.commit()
        flash(f"Added subscription: {name}", "success")
        return redirect(url_for('index'))

    subs = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.renewal_date.asc()).all()
    total_cost = sum(s.price for s in subs)

    today = datetime.today().date()
    week_ahead = today + timedelta(days=7)
    upcoming_names = [s.name for s in subs if today <= s.renewal_date <= week_ahead]

    return render_template('index.html', subscriptions=subs, total_cost=total_cost, upcoming=upcoming_names)

@app.route('/delete/<int:sub_id>', methods=['POST'])
@login_required
def delete_sub(sub_id):
    sub = Subscription.query.get_or_404(sub_id)
    if sub.user_id != current_user.id:
        flash("Not allowed", "warning")
        return redirect(url_for('index'))
    db.session.delete(sub)
    db.session.commit()
    flash("Subscription deleted", "info")
    return redirect(url_for('index'))

# --- Upgrade / Stripe Checkout ---
@app.route('/upgrade', methods=['GET'])
@login_required
def upgrade():
    if current_user.is_premium:
        flash("You're already Premium. Unlimited subscriptions enabled.", "info")
        return redirect(url_for('index'))
    return render_template('upgrade.html')

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    price_id = STRIPE_PRICE_ID
    if not price_id or not stripe.api_key:
        flash("Payment not configured. Missing STRIPE keys.", "warning")
        return redirect(url_for('upgrade'))

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=url_for('upgrade_success', _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for('upgrade', _external=True),
            customer_email=current_user.email
        )
        return redirect(session.url, code=303)
    except Exception as e:
        flash(f"Stripe error: {e}", "warning")
        return redirect(url_for('upgrade'))

@app.route('/upgrade/success', methods=['GET'])
@login_required
def upgrade_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash("Missing session id.", "warning")
        return redirect(url_for('index'))
    try:
        cs = stripe.checkout.Session.retrieve(session_id)
        if cs.get("payment_status") == "paid":
            current_user.is_premium = True
            db.session.commit()
            flash("✅ Upgrade successful! Your account is now Premium.", "success")
        else:
            flash("Payment not completed yet.", "warning")
    except Exception as e:
        flash(f"Could not verify payment: {e}", "warning")
    return redirect(url_for('index'))

if __name__ == "__main__":
    app.run(debug=True)
